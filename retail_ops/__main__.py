"""Run with python -m retail_ops, including from an unpacked repository."""

import argparse
import json
from pathlib import Path

from .core import report_zip
from .server import demo_payload, run_payload, serve


def main():
    parser = argparse.ArgumentParser(description="Проверка товарных данных на своём компьютере")
    sub = parser.add_subparsers(dest="command")
    web = sub.add_parser("serve", help="Открыть локальный интерфейс")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true")
    demo = sub.add_parser("demo", help="Проверить синтетические примеры")
    demo.add_argument("--output", default="outputs/demo-report.zip")
    check = sub.add_parser("check", help="Проверить свои CSV/XLSX")
    check.add_argument("supplies", nargs="+")
    check.add_argument("--reference")
    check.add_argument("--codes")
    check.add_argument("--require-ntin", action="store_true")
    check.add_argument("--mapping", help="JSON: стандартное поле → список названий столбцов")
    check.add_argument("--output", default="outputs/report.zip")
    args = parser.parse_args()
    if args.command in (None, "serve"):
        serve(getattr(args, "port", 8765), not getattr(args, "no_browser", False))
        return
    if args.command == "demo":
        result = run_payload(demo_payload())
        result.pop("zip_base64")
    else:
        import base64

        def file(path):
            p = Path(path)
            return {"name": p.name, "content": base64.b64encode(p.read_bytes()).decode()}

        payload = {"supplies": [file(path) for path in args.supplies], "require_ntin": args.require_ntin}
        for kind in ("reference", "codes"):
            if getattr(args, kind):
                payload[kind] = file(getattr(args, kind))
        if args.mapping:
            payload["aliases"] = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
        result = run_payload(payload)
        result.pop("zip_base64")
    output = Path(args.output)
    if output.exists():
        parser.error(f"Файл уже существует: {output}. Выберите другой --output.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(report_zip(result))
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"Отчёт сохранён: {output.resolve()}")


if __name__ == "__main__":
    main()
