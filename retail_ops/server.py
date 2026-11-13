"""Single-user loopback server. All input processing stays on this computer."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import secrets
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import MAX_FILE, analyze, estimate_savings, photo_archive, read_table, report_zip

ROOT = Path(__file__).parent
MAX_REQUEST = 32 * 1024 * 1024


def decode_file(item: dict):
    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
        raise ValueError("Некорректное описание файла.")
    name = item["name"]
    if not name or Path(name).name != name or "\\" in name or len(name) > 200:
        raise ValueError("Недопустимое имя файла.")
    try:
        data = base64.b64decode(item.get("content", ""), validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("Файл повреждён при передаче.") from exc
    if len(data) > MAX_FILE:
        raise ValueError("Максимальный размер одного файла 15 МБ.")
    return name, data


def run_payload(payload: dict) -> dict:
    files = payload.get("supplies", [])
    if not isinstance(files, list) or not 1 <= len(files) <= 20:
        raise ValueError("Загрузите от 1 до 20 файлов поставок.")
    aliases = payload.get("aliases")
    if aliases is not None:
        if not isinstance(aliases, dict) or any(not isinstance(k, str) or not isinstance(v, list) or any(not isinstance(x, str) for x in v) for k, v in aliases.items()):
            raise ValueError("Сопоставление столбцов должно быть объектом: поле → список заголовков.")
        # Custom mappings extend defaults; cannot reinterpret identifiers as free text.
        from .core import ALIASES
        if set(aliases) - set(ALIASES):
            raise ValueError("Неизвестное стандартное поле в сопоставлении.")
        aliases = {k: [*v, *aliases.get(k, [])] for k, v in ALIASES.items()}
    decoded = [decode_file(f) for f in files]
    tables = [read_table(name, data, aliases) for name, data in decoded]
    optional, hashes = {}, {f"supplies/{name}": hashlib.sha256(data).hexdigest() for name, data in decoded}
    for field in ("reference", "codes"):
        entry = payload.get(field)
        if entry:
            name, data = decode_file(entry)
            optional[field] = read_table(name, data, aliases)
            hashes[f"{field}/{name}"] = hashlib.sha256(data).hexdigest()
    result = analyze(tables, require_ntin=bool(payload.get("require_ntin", False)), **optional)
    result["file_sha256"] = hashes
    return {**result, "zip_base64": base64.b64encode(report_zip(result)).decode()}


def demo_payload() -> dict:
    def file(name):
        return {"name": name, "content": base64.b64encode((ROOT / "demo" / name).read_bytes()).decode()}
    return {"supplies": [file("supplier_a.csv"), file("supplier_b.csv")],
            "reference": file("brand_reference.csv"), "codes": file("marking_codes.csv")}


def serve(port: int = 8765, open_browser: bool = True):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Product names, codes, and bodies never enter the server logs.
            pass

        def send_data(self, status, data, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def json_result(self, status, obj):
            self.send_data(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.valid_host():
                return self.json_result(403, {"error": "Разрешён только локальный адрес."})
            if self.path == "/":
                page = (ROOT / "web" / "index.html").read_text().replace("__TOKEN__", token)
                return self.send_data(200, page.encode(), "text/html; charset=utf-8")
            if self.path in {"/app.js", "/style.css"}:
                path = ROOT / "web" / self.path[1:]
                mime = "text/javascript" if path.suffix == ".js" else "text/css"
                return self.send_data(200, path.read_bytes(), mime + "; charset=utf-8")
            if self.path == "/templates.zip":
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted((ROOT / "demo").glob("*.csv")):
                        archive.writestr(path.name, path.read_bytes())
                return self.send_data(200, buffer.getvalue(), "application/zip")
            self.json_result(404, {"error": "Страница не найдена."})

        def do_POST(self):
            origin = self.headers.get("Origin")
            allowed_origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if not self.valid_host() or self.headers.get("X-Local-Token") != token or (origin and origin not in allowed_origins):
                return self.json_result(403, {"error": "Запрос отклонён. Откройте локальную страницу заново."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST:
                    raise ValueError("Размер запроса должен быть от 1 байта до 32 МБ.")
                self.connection.settimeout(30)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Ожидается JSON-объект.")
                if self.path == "/api/analyze":
                    result = run_payload(payload)
                elif self.path == "/api/demo":
                    result = run_payload(demo_payload())
                elif self.path == "/api/estimate":
                    result = estimate_savings(payload.get("tasks", []), float(payload.get("days", 20)))
                elif self.path == "/api/photos":
                    name, data = decode_file(payload.get("manifest", {}))
                    _, photo_data = decode_file(payload.get("photos", {}))
                    content, audit = photo_archive(read_table(name, data), photo_data)
                    result = {"files": audit, "zip_base64": base64.b64encode(content).decode()}
                else:
                    return self.json_result(404, {"error": "Метод не найден."})
                self.json_result(200, result)
            except (ValueError, KeyError, TypeError, zipfile.BadZipFile, OSError) as exc:
                self.json_result(400, {"error": str(exc)})
            except Exception:
                self.json_result(500, {"error": "Не удалось обработать файл. Проверьте формат и целостность; исходные файлы не изменены."})

    class LocalServer(ThreadingHTTPServer):
        daemon_threads = True

    with LocalServer(("127.0.0.1", port), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"Retail Ops Assistant: {url}\nДля остановки нажмите Ctrl+C.", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nПрограмма остановлена.")
