"""Web UI for training."""

import io
import logging
import shutil
import tarfile
import tempfile
import time
from collections.abc import Collection, Iterable
from logging.handlers import QueueHandler
from pathlib import Path
from queue import Queue
from typing import Optional
from urllib.request import urlopen

from flask import Flask, Response, redirect, render_template, request
from flask import url_for as flask_url_for
from rhasspy_speech.const import LangSuffix
from rhasspy_speech.g2p import LexiconDatabase, get_sounds_like, guess_pronunciations
from rhasspy_speech.tools import KaldiTools
from rhasspy_speech.train import train_model as rhasspy_train_model
from werkzeug.middleware.proxy_fix import ProxyFix
from yaml import SafeDumper, safe_dump, safe_load

from .hass_api import get_exposed_dict
from .models import MODELS
from .shared import AppState

_DIR = Path(__file__).parent
_LOGGER = logging.getLogger(__name__)


DOWNLOAD_CHUNK_SIZE = 1024 * 10


def ingress_url_for(endpoint, **values):
    """Custom url_for that includes X-Ingress-Path dynamically."""
    ingress_path = request.headers.get("X-Ingress-Path", "")
    base_url = flask_url_for(endpoint, **values)
    # Prepend the ingress path if it's present
    return f"{ingress_path}{base_url}" if ingress_path else base_url


def get_app(state: AppState) -> Flask:
    app = Flask(
        "rhasspy_speech",
        template_folder=str(_DIR / "templates"),
        static_folder=str(_DIR / "static"),
    )

    if state.settings.hass_ingress:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)  # type: ignore[assignment]
        app.jinja_env.globals["url_for"] = ingress_url_for

    @app.route("/")
    def index():
        downloaded_models = {
            m.id for m in MODELS.values() if (state.settings.models_dir / m.id).is_dir()
        }
        return render_template(
            "index.html",
            available_models=MODELS,
            downloaded_models=downloaded_models,
        )

    @app.route("/manage")
    def manage():
        model_id = request.args["id"]
        suffix = request.args.get("suffix")

        sentences_path = state.settings.sentences_path(model_id, suffix)
        return render_template(
            "manage.html",
            model_id=model_id,
            suffix=suffix,
            suffixes=state.settings.get_suffixes(model_id),
            has_sentences=sentences_path.exists(),
        )

    @app.route("/download")
    def download():
        model_id = request.args["id"]
        return render_template("download.html", model_id=model_id)

    @app.route("/api/download", methods=["POST"])
    def api_download() -> Response:
        model_id = request.args["id"]

        def download_model() -> Iterable[str]:
            try:
                model = MODELS.get(model_id)
                assert model is not None, f"Unknown model: {model_id}"
                with urlopen(
                    model.url
                ) as model_response, tempfile.TemporaryDirectory() as temp_dir:
                    total_bytes: Optional[int] = None
                    content_length = model_response.getheader("Content-Length")
                    if content_length:
                        total_bytes = int(content_length)
                        yield f"Expecting {total_bytes} byte(s)\n"

                    last_report_time = time.monotonic()
                    model_path = Path(temp_dir) / "model.tar.gz"
                    bytes_downloaded = 0
                    with open(model_path, "wb") as model_file:
                        chunk = model_response.read(DOWNLOAD_CHUNK_SIZE)
                        while chunk:
                            model_file.write(chunk)
                            bytes_downloaded += len(chunk)
                            current_time = time.monotonic()
                            if (current_time - last_report_time) > 1:
                                if (total_bytes is not None) and (total_bytes > 0):
                                    yield f"{int((bytes_downloaded / total_bytes) * 100)}%\n"
                                else:
                                    yield f"Bytes downloaded: {bytes_downloaded}\n"
                                last_report_time = current_time
                            chunk = model_response.read(DOWNLOAD_CHUNK_SIZE)

                    yield "Download complete\n"
                    state.settings.models_dir.mkdir(parents=True, exist_ok=True)
                    with tarfile.open(model_path, "r:gz") as model_tar_file:
                        model_tar_file.extractall(state.settings.models_dir)
                    yield "Model extracted\n"
                    yield "Return to models page to continue\n"
            except Exception as err:
                yield f"ERROR: {err}"

        return Response(download_model(), content_type="text/plain")

    @app.route("/api/train", methods=["POST"])
    async def api_train() -> Response:
        model_id = request.args["id"]
        suffix = request.args.get("suffix")

        logger = logging.getLogger("rhasspy_speech")
        logger.setLevel(logging.DEBUG)
        log_queue: "Queue[Optional[logging.LogRecord]]" = Queue()
        handler = QueueHandler(log_queue)
        logger.addHandler(handler)
        text = "Training started\n"

        try:
            await train_model(state, model_id, suffix, log_queue)
            while True:
                log_item = log_queue.get()
                if log_item is None:
                    break

                text += log_item.getMessage() + "\n"
            text += "Training complete\n"
        except Exception as err:
            text += f"ERROR: {err}"
        finally:
            logger.removeHandler(handler)

        return Response(text, content_type="text/plain")

    @app.route("/sentences", methods=["GET", "POST"])
    def sentences():
        model_id = request.args["id"]
        suffix = request.args.get("suffix")
        sentences = ""
        sentences_path = state.settings.sentences_path(model_id, suffix)

        if request.method == "POST":
            sentences = request.form["sentences"]
            try:
                with io.StringIO(sentences) as sentences_file:
                    sentences_dict = safe_load(sentences_file)
                    assert "sentences" in sentences_dict, "Missing sentences block"
                    assert sentences_dict["sentences"], "No sentences"

                # Success
                sentences_path.parent.mkdir(parents=True, exist_ok=True)
                sentences_path.write_text(sentences, encoding="utf-8")

                if state.settings.hass_ingress:
                    return redirect(
                        ingress_url_for("manage", id=model_id, suffix=suffix)
                    )

                return redirect(flask_url_for("manage", id=model_id, suffix=suffix))
            except Exception as err:
                return render_template(
                    "sentences.html",
                    model_id=model_id,
                    sentences=sentences,
                    error=err,
                )

        elif sentences_path.exists():
            sentences = sentences_path.read_text(encoding="utf-8")

        return render_template(
            "sentences.html", model_id=model_id, suffix=suffix, sentences=sentences
        )

    @app.route("/delete", methods=["GET", "POST"])
    def delete():
        model_id = request.args["id"]
        suffix = request.args.get("suffix")

        model_data_dir = state.settings.model_data_dir(model_id)
        if model_data_dir.is_dir():
            shutil.rmtree(model_data_dir)

        model_train_dir = state.settings.model_train_dir(model_id, suffix)
        if model_train_dir.is_dir():
            shutil.rmtree(model_train_dir)

        return redirect(ingress_url_for("index"))

    @app.route("/api/hass_exposed", methods=["POST"])
    async def api_hass_exposed() -> str:
        if state.settings.hass_token is None:
            return "No Home Assistant token"

        exposed_dict = await get_exposed_dict(
            state.settings.hass_token, state.settings.hass_websocket_uri
        )
        SafeDumper.ignore_aliases = lambda *args: True  # type: ignore[assignment]
        with io.StringIO() as hass_exposed_file:
            safe_dump({"lists": exposed_dict}, hass_exposed_file, sort_keys=False)
            return hass_exposed_file.getvalue()

    @app.route("/words", methods=["GET", "POST"])
    def words():
        model_id = request.args["id"]
        words_str = ""
        found = ""
        guessed = ""

        if request.method == "POST":
            words_str = request.form["words"]
            lexicon = LexiconDatabase(
                state.settings.models_dir / model_id / "lexicon.db"
            )

            if "*" in words_str:
                # pylint: disable=protected-access
                cur = lexicon._conn.execute(
                    "SELECT word, phonemes FROM word_phonemes WHERE word LIKE ?",
                    (words_str.replace("*", "%"),),
                )
                for row in cur:
                    found += f'{row[0]}: "/{row[1]}/"\n'

            else:
                words = words_str.split()
                missing_words = set()
                for word in words:
                    if "[" in word:
                        word_prons = get_sounds_like([word], lexicon)
                    else:
                        word_prons = lexicon.lookup(word)

                    if word_prons:
                        for word_pron in word_prons:
                            phonemes = " ".join(word_pron)
                            found += f'{word}: "/{phonemes}/"\n'
                    else:
                        missing_words.add(word)

                if missing_words:
                    for word, phonemes in guess_pronunciations(
                        missing_words,
                        state.settings.models_dir / model_id / "g2p.fst",
                        state.settings.tools_dir / "phonetisaurus",
                    ):
                        guessed += f'{word}: "/{phonemes}/"\n'

        return render_template(
            "words.html",
            model_id=model_id,
            words=words_str,
            found=found,
            guessed=guessed,
        )

    @app.errorhandler(Exception)
    async def handle_error(err):
        """Return error as text."""
        return (f"{err.__class__.__name__}: {err}", 500)

    return app


# -----------------------------------------------------------------------------


async def train_model(
    state: AppState, model_id: str, suffix: Optional[str], log_queue: Queue
):
    try:
        _LOGGER.info("Training %s (suffix=%s)", model_id, suffix)
        start_time = time.monotonic()
        # TODO
        # sentences_path = state.settings.sentences_path(model_id, suffix)
        sentences_path = Path("/home/hansenm/opt/wyoming-rhasspy-speech/wyoming_rhasspy_speech/sentences/en.yaml")
        model_train_dir = state.settings.model_train_dir(model_id, suffix)
        model_train_dir.mkdir(parents=True, exist_ok=True)

        lang_suffixes: Collection[LangSuffix]
        if state.settings.decode_mode == "grammar":
            lang_suffixes = (LangSuffix.GRAMMAR,)
        elif state.settings.decode_mode == "arpa_rescore":
            lang_suffixes = (LangSuffix.ARPA, LangSuffix.ARPA_RESCORE)
        else:
            lang_suffixes = (LangSuffix.ARPA,)

        language = model_id.split("-")[0].split("_")[0]
        await rhasspy_train_model(
            language=language,
            sentence_files=[sentences_path],
            model_dir=state.settings.models_dir / model_id,
            train_dir=model_train_dir,
            tools=KaldiTools.from_tools_dir(state.settings.tools_dir),
            lang_suffixes=lang_suffixes,
            rescore_order=state.settings.arpa_rescore_order,
        )
        _LOGGER.debug(
            "Training completed in %s second(s)", time.monotonic() - start_time
        )
    except Exception:
        _LOGGER.exception("Unexpected error while training")
    finally:
        log_queue.put(None)
