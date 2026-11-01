"""Lecture d'un envoi multipart : les champs en mémoire, le fichier `file` sur disque, bloc par bloc.

Le corps n'est jamais lu en entier avant d'être jugé : un envoi trop gros est refusé dès son `Content-Length`, ou dès
que les octets reçus dépassent la limite. Rien n'est journalisé ici.
"""

from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import Request

from dictee.errors import ApiError

MIB = 1024 * 1024
# Un champ du formulaire (la consigne `prompt`, par exemple) reste en mémoire : sa taille et leur nombre sont bornés.
MAX_FIELD_BYTES = MIB
MAX_PARTS = 100


def payload_too_large(limit_mb: int) -> ApiError:
    """Erreur 413 d'un corps plus grand que la limite."""
    message = f"The request body is larger than the limit of {limit_mb} MB."
    return ApiError(413, message, param="file", code="payload_too_large")


class MultipartReader:
    """Lit un corps multipart bloc par bloc : les champs restent en mémoire, la partie `file` va sur disque.

    `write` reçoit les blocs du corps, `finish` clôt la lecture et `close` ferme le fichier ; `fields` donne alors
    toutes les valeurs de chaque champ, dans l'ordre d'arrivée, et `has_file` dit si la partie `file` est venue. Les
    autres parties qui portent un fichier sont acceptées et ignorées. `on_file` reçoit les champs déjà lus quand la
    partie `file` commence, avant que son premier octet soit écrit.
    """

    def __init__(self, boundary: bytes, path: Path, on_file: Callable[[dict[str, list[str]]], None]) -> None:
        self.fields: dict[str, list[str]] = {}
        self.has_file = False
        self._path = path
        self._on_file = on_file
        self._parts = 0
        self._name_chunks = b""
        self._value_chunks = b""
        self._disposition = b""
        self._field_name = ""
        self._value = bytearray()
        self._file: BinaryIO | None = None
        self._ignored = False
        self._parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self._part_begin,
                "on_header_field": self._header_field,
                "on_header_value": self._header_value,
                "on_header_end": self._header_end,
                "on_headers_finished": self._headers_finished,
                "on_part_data": self._part_data,
                "on_part_end": self._part_end,
            },
        )

    def write(self, chunk: bytes) -> None:
        self._parser.write(chunk)

    def finish(self) -> None:
        self._parser.finalize()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _part_begin(self) -> None:
        self._parts += 1
        if self._parts > MAX_PARTS:
            raise ApiError(400, "The form has too many parts.")
        self._disposition = b""
        self._value.clear()
        self._ignored = False

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._name_chunks += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value_chunks += data[start:end]

    def _header_end(self) -> None:
        if self._name_chunks.lower() == b"content-disposition":
            self._disposition = self._value_chunks
        self._name_chunks = self._value_chunks = b""

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        self._field_name = options.get(b"name", b"").decode("utf-8", "replace")
        if b"filename" not in options:
            return
        if self._field_name == "file" and not self.has_file:
            self._on_file(self.fields)
            self._file = self._path.open("wb")
            self.has_file = True
        else:
            self._ignored = True

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._file is not None:
            self._file.write(data[start:end])
        elif not self._ignored:
            self._value += data[start:end]
            if len(self._value) > MAX_FIELD_BYTES:
                raise ApiError(400, "A form field is too large.")

    def _part_end(self) -> None:
        if self._file is not None:
            self.close()
        elif not self._ignored:
            self.fields.setdefault(self._field_name, []).append(self._value.decode("utf-8", "replace"))


async def read_upload(
    request: Request, path: Path, limit_mb: int, on_file: Callable[[dict[str, list[str]]], None]
) -> tuple[dict[str, list[str]], bool]:
    """Lit le formulaire de la requête et écrit son fichier à `path` ; rend les champs et si un fichier est arrivé.

    Le corps ne dépasse pas `limit_mb` Mo : un `Content-Length` plus grand est refusé avant toute lecture, et la
    lecture s'arrête sur la même erreur 413 dès que les octets reçus dépassent la limite, sans avoir tout reçu. Un
    corps qui n'est pas du multipart, ou dont le multipart est illisible, donne une erreur 400. `on_file` est appelée
    quand la partie `file` commence (voir `MultipartReader`) et peut lever une `ApiError` pour refuser l'envoi.
    """
    limit = limit_mb * MIB
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        declared = 0
    if declared > limit:
        raise payload_too_large(limit_mb)
    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if content_type.lower() != b"multipart/form-data" or not boundary:
        raise ApiError(400, "Send the request as multipart/form-data.")
    reader = MultipartReader(boundary, path, on_file)
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise payload_too_large(limit_mb)
            reader.write(chunk)
        reader.finish()
    except FormParserError:
        raise ApiError(400, "The multipart body is malformed.") from None
    finally:
        reader.close()
    return reader.fields, reader.has_file
