"""Deterministic checks. No network calls or production-system writes."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any

MAX_FILE = 15 * 1024 * 1024
MAX_ROWS = 30_000
MAX_EXPANDED = 80 * 1024 * 1024
FIELDS = ("shipment_id", "supplier", "sku", "brand", "name", "color", "size",
          "quantity", "gtin", "ntin", "material", "country")
REQUIRED = ("shipment_id", "supplier", "sku", "brand", "name", "color", "size", "quantity", "gtin")
ALIASES = {
    "shipment_id": ["поставка", "номер поставки", "заказ", "шнс заказа"],
    "supplier": ["поставщик"], "sku": ["артикул", "код товара"],
    "brand": ["бренд"], "name": ["наименование", "название"],
    "color": ["цвет"], "size": ["размер"], "quantity": ["количество", "кол-во", "qty"],
    "gtin": ["штрихкод", "штрих-код"], "ntin": [], "material": ["материал", "состав"],
    "country": ["страна"], "km": ["км", "код маркировки"],
    "source_url": ["источник", "ссылка"], "checked_at": ["дата проверки"],
    "original_name": ["исходный файл"], "view": ["ракурс"],
}
IDENTIFIERS = {"shipment_id", "sku", "gtin", "ntin", "km"}


def norm(value: Any) -> str:
    """Normalize display text, never identifier contents."""
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def key(value: str) -> str:
    return norm(value).casefold()


def canonical_header(value: str, aliases: dict | None = None) -> str:
    match = key(value)
    for canonical, candidates in (aliases or ALIASES).items():
        if match in {key(canonical), *(key(c) for c in candidates)}:
            return canonical
    return match


@dataclass
class Table:
    rows: list[dict[str, str]]
    headers: list[str]
    source: str
    # Absolute source rows with unsafe identifier cell types or formulas.
    cell_issues: dict[int, list[tuple[str, str]]] = field(default_factory=dict)


def validate_archive(data: bytes) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(io.BytesIO(data))
    members = archive.infolist()
    if len(members) > 5_000 or sum(m.file_size for m in members) > MAX_EXPANDED:
        archive.close()
        raise ValueError("Архив слишком большой после распаковки (лимит 80 МБ / 5000 файлов).")
    return archive


def read_table(name: str, data: bytes, aliases: dict | None = None) -> Table:
    if len(data) > MAX_FILE:
        raise ValueError(f"{name}: файл больше 15 МБ.")
    suffix = PurePosixPath(name).suffix.casefold()
    cell_issues: dict[int, list[tuple[str, str]]] = defaultdict(list)
    if suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError("Для XLSX установите дополнение: pip install '.[excel]'. CSV работает без него.") from exc
        with validate_archive(data):
            pass
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
        try:
            if len(book.worksheets) != 1:
                raise ValueError(f"{name}: нужен файл с одним листом. Выгрузите нужный лист отдельно.")
            sheet = book.worksheets[0]
            if sheet.max_column > 100:
                raise ValueError(f"{name}: максимум 100 столбцов.")
            cells = sheet.iter_rows()
            first = next(cells, ())
            raw_headers = [str(c.value or "").strip() for c in first]
            headers = [canonical_header(h, aliases) for h in raw_headers]
            raw_rows = []
            for row_number, row in enumerate(cells, 2):
                if row_number > MAX_ROWS + 1:
                    raise ValueError(f"{name}: максимум {MAX_ROWS} строк.")
                if all(c.value is None for c in row):
                    continue
                values = []
                for column, cell in enumerate(row):
                    label = headers[column]
                    if cell.data_type == "f":
                        cell_issues[row_number].append((label, "Формула: замените проверенным значением."))
                    elif label in IDENTIFIERS and cell.value is not None and not isinstance(cell.value, str):
                        cell_issues[row_number].append((label, "Идентификатор хранится числом: исходные нули могли потеряться. Нужна текстовая выгрузка."))
                    values.append("" if cell.value is None else str(cell.value))
                raw_rows.append((row_number, values))
        finally:
            book.close()
    elif suffix == ".csv":
        try:
            decoded = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = data.decode("cp1251")
        # Explicitly distinguish delimiter-only detection from field contents.
        first_line = decoded.splitlines()[0] if decoded.splitlines() else ""
        delimiter = max((";", ",", "\t"), key=first_line.count)
        reader = csv.reader(io.StringIO(decoded, newline=""), delimiter=delimiter)
        raw_headers = next(reader, [])
        headers = [canonical_header(h, aliases) for h in raw_headers]
        raw_rows = []
        for row_number, values in enumerate(reader, 2):
            if row_number > MAX_ROWS + 1:
                raise ValueError(f"{name}: максимум {MAX_ROWS} строк.")
            if not any(v.strip() for v in values):
                continue
            raw_rows.append((row_number, values))
    else:
        raise ValueError(f"{name}: поддерживаются только CSV и XLSX.")
    if not headers or any(not h for h in headers):
        raise ValueError(f"{name}: пустой заголовок столбца.")
    if len(headers) > 100 or len(set(headers)) != len(headers):
        raise ValueError(f"{name}: повторяющиеся заголовки (в том числе после сопоставления) или больше 100 столбцов.")
    rows = []
    for source_row, values in raw_rows:
        if len(values) != len(headers):
            raise ValueError(f"{name}, строка {source_row}: число значений не совпадает с заголовками.")
        record = {}
        for header, value in zip(headers, values):
            # KM can contain ASCII GS and spaces: preserve the full exact string.
            record[header] = value if header == "km" else value.strip() if header in IDENTIFIERS else norm(value)
        record["_source"] = name
        record["_row"] = str(source_row)
        rows.append(record)
    return Table(rows, headers, name, dict(cell_issues))


def gtin_valid(value: str) -> bool:
    if len(value) not in (8, 12, 13, 14) or not re.fullmatch(r"[0-9]+", value) or set(value) == {"0"}:
        return False
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(value[:-1])))
    return (10 - total % 10) % 10 == int(value[-1])


def gtin_key(value: str) -> str:
    return value.zfill(14) if gtin_valid(value) else value


def positive_quantity(value: str) -> int | None:
    try:
        number = Decimal(value.replace(",", "."))
        if number.is_finite() and 0 < number <= 10_000_000 and number == number.to_integral_value():
            return int(number)
    except (InvalidOperation, ValueError):
        pass
    return None


def variant(row: dict) -> tuple:
    # Article is exact; a different brand can have the same supplier article.
    return (key(row.get("brand", "")), row.get("sku", ""), key(row.get("color", "")), key(row.get("size", "")))


def issue(row: dict, severity: str, code: str, field_name: str, message: str, expected: str = "") -> dict:
    return {"source": row.get("_source", ""), "row": row.get("_row", ""),
            "shipment_id": row.get("shipment_id", ""), "sku": row.get("sku", ""),
            "severity": severity, "code": code, "field": field_name, "message": message,
            "expected": expected}


def analyze(tables: list[Table], reference: Table | None = None, codes: Table | None = None,
            require_ntin: bool = False) -> dict:
    rows = [dict(row) for table in tables for row in table.rows]
    if not rows:
        raise ValueError("В файлах поставок нет строк данных.")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"Во всех поставках вместе допускается до {MAX_ROWS} строк.")
    issues = []
    bad: set[tuple[str, str]] = set()

    def add(row, severity, code, field_name, message, expected=""):
        issues.append(issue(row, severity, code, field_name, message, expected))
        if severity == "error":
            bad.add((row["_source"], row["_row"]))

    sources = [t.source for t in tables]
    if len(sources) != len(set(sources)):
        raise ValueError("Имена входных файлов должны различаться, чтобы сохранить происхождение строк.")
    source_tables = {table.source: table for table in tables}
    duplicate_lines = defaultdict(list)
    gtin_variants = defaultdict(list)
    variant_gtins = defaultdict(list)
    for row in rows:
        for required in REQUIRED:
            if not row.get(required):
                add(row, "error", "MISSING", required, "Обязательное поле не заполнено.")
        for field_name, message in source_tables[row["_source"]].cell_issues.get(int(row["_row"]), []):
            add(row, "error", "UNSAFE_CELL", field_name, message)
        if row.get("gtin") and not gtin_valid(row["gtin"]):
            add(row, "error", "GTIN_INVALID", "gtin", "Неверная длина, символы или контрольная цифра GTIN.")
        if row.get("quantity") and positive_quantity(row["quantity"]) is None:
            add(row, "error", "QUANTITY_INVALID", "quantity", "Нужно положительное целое количество, максимум 10 000 000.")
        if not row.get("ntin"):
            add(row, "error" if require_ntin else "warning", "NTIN_MISSING", "ntin", "NTIN отсутствует. Необходимость проверяется по процессу компании.")
        duplicate_lines[(row.get("shipment_id", ""), *variant(row))].append(row)
        if gtin_valid(row.get("gtin", "")):
            gtin_variants[gtin_key(row["gtin"])].append(row)
            variant_gtins[variant(row)].append(row)
    for group in duplicate_lines.values():
        if len(group) > 1:
            for row in group:
                add(row, "error", "DUPLICATE_LINE", "sku", "Повтор варианта товара внутри поставки. Строки сохранены, объединение требует решения.")
    for group in gtin_variants.values():
        if len({variant(r) for r in group}) > 1:
            for row in group:
                add(row, "error", "GTIN_CONFLICT", "gtin", "Один GTIN указан для разных вариантов товара.")
    for group in variant_gtins.values():
        if len({gtin_key(r["gtin"]) for r in group}) > 1:
            for row in group:
                add(row, "error", "VARIANT_GTIN_CONFLICT", "gtin", "У одного варианта товара разные GTIN. Требуется проверка упаковки/карточки.")
    comparisons = []
    if reference is not None:
        needed = {"sku", "brand", "color", "size", "source_url", "checked_at"}
        if not needed.issubset(reference.headers):
            raise ValueError("Эталон должен содержать sku, brand, color, size, source_url, checked_at.")
        index = defaultdict(list)
        for ref in reference.rows:
            index[variant(ref)].append(ref)
        for row in rows:
            matches = index.get(variant(row), [])
            if len(matches) != 1:
                add(row, "warning", "REFERENCE_AMBIGUOUS" if matches else "REFERENCE_MISSING", "sku",
                    "Нет однозначного совпадения в эталоне. Автоматическая сверка не выполнена.")
                continue
            ref = matches[0]
            unsafe = reference.cell_issues.get(int(ref["_row"]), [])
            if unsafe or not re.match(r"^https://[^/\s]+", ref.get("source_url", "")) or not ref.get("checked_at"):
                add(row, "error", "REFERENCE_UNVERIFIED", "source_url", "Эталон: нужны текстовые идентификаторы, URL источника HTTPS и дата проверки; формулы недопустимы.")
                continue
            for field_name in ("name", "material", "country", "gtin", "ntin"):
                expected = ref.get(field_name, "")
                if not expected:
                    continue
                actual = row.get(field_name, "")
                same = gtin_key(actual) == gtin_key(expected) if field_name == "gtin" else key(actual) == key(expected)
                comparisons.append({"source": row["_source"], "row": row["_row"], "sku": row.get("sku", ""),
                                    "field": field_name, "actual": actual, "expected": expected,
                                    "status": "match" if same else "difference", "source_url": ref["source_url"],
                                    "checked_at": ref["checked_at"]})
                if not same:
                    add(row, "error", "REFERENCE_DIFFERENCE", field_name, "Расхождение с предоставленным эталоном. Исходное значение сохранено.", expected)
    else:
        for row in rows:
            add(row, "warning", "REFERENCE_NOT_SUPPLIED", "", "Эталон не загружен: сверка с данными бренда не выполнена.")

    ready = [row for row in rows if (row["_source"], row["_row"]) not in bad]
    requests = defaultdict(int)
    for row in ready:
        requests[(row["shipment_id"], gtin_key(row["gtin"]))] += positive_quantity(row["quantity"]) or 0
    request_rows = [{"shipment_id": s, "gtin": g, "quantity": q, "status": "draft"}
                    for (s, g), q in sorted(requests.items())]
    reconciliation, code_issues = reconcile_codes(rows, codes)
    issues.extend(code_issues)
    shipment_rows = []
    shipments = defaultdict(list)
    for row in rows:
        shipments[row.get("shipment_id", "")].append(row)
    for shipment, group in sorted(shipments.items()):
        shipment_rows.append({"shipment_id": shipment, "lines": len(group),
                              "quantity_known": sum(positive_quantity(r.get("quantity", "")) or 0 for r in group),
                              "quantity_invalid_lines": sum(positive_quantity(r.get("quantity", "")) is None for r in group),
                              "blocked_lines": sum((r["_source"], r["_row"]) in bad for r in group),
                              "suppliers": ", ".join(sorted({r.get("supplier", "") for r in group}))})
    normalized = [{**row, "_status": "blocked" if (row["_source"], row["_row"]) in bad else "draft_ready"} for row in rows]
    return {"summary": {"rows": len(rows), "shipments": len(shipments), "draft_ready": len(ready),
                        "blocked": len(rows) - len(ready), "errors": sum(i["severity"] == "error" for i in issues),
                        "warnings": sum(i["severity"] == "warning" for i in issues),
                        "reference_supplied": reference is not None, "codes_supplied": codes is not None},
            "reports": {"products": normalized, "product_card_drafts": [{f: r.get(f, "") for f in FIELDS} for r in ready],
                        "issues": issues, "reference_comparison": comparisons,
                        "shipments": shipment_rows, "km_request_drafts": request_rows,
                        "km_reconciliation": reconciliation},
            "semantic_input_sha256": {f"supplies/{t.source}": t_hash(t) for t in tables}
                | ({f"reference/{reference.source}": t_hash(reference)} if reference else {})
                | ({f"codes/{codes.source}": t_hash(codes)} if codes else {})}


def t_hash(table: Table) -> str:
    # Semantic digest; not a claim to hash the original file bytes.
    return hashlib.sha256(json.dumps(table.rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def reconcile_codes(rows: list[dict], codes: Table | None) -> tuple[list[dict], list[dict]]:
    demand = defaultdict(int)
    ambiguous = set()
    for row in rows:
        pair = (row.get("shipment_id", ""), gtin_key(row.get("gtin", "")))
        quantity = positive_quantity(row.get("quantity", ""))
        if not pair[0] or not gtin_valid(pair[1]) or quantity is None:
            ambiguous.add(pair)
        demand[pair] += quantity or 0
    if codes is None:
        return [{"shipment_id": s, "gtin": g, "quantity": q, "codes": "", "difference": "", "status": "not_checked"}
                for (s, g), q in sorted(demand.items())], []
    if not {"shipment_id", "gtin", "km"}.issubset(codes.headers):
        raise ValueError("Файл КМ должен содержать shipment_id, gtin, km.")
    counts = Counter(r.get("km", "") for r in codes.rows if r.get("km", ""))
    accepted: dict[tuple, set] = defaultdict(set)
    issues = []
    invalid_pairs = set()
    for row in codes.rows:
        pair = (row.get("shipment_id", ""), gtin_key(row.get("gtin", "")))
        km = row.get("km", "")
        reason = ""
        if codes.cell_issues.get(int(row["_row"])):
            reason = "Небезопасные типы ячеек или формулы в файле КМ."
        elif not pair[0] or not gtin_valid(row.get("gtin", "")) or not km.strip():
            reason = "Не заполнена поставка/КМ или неверный GTIN."
        elif counts[km] > 1:
            reason = "КМ повторяется. Все вхождения исключены из количества до проверки."
        if reason:
            issues.append(issue(row, "error", "KM_INVALID", "km", reason))
            invalid_pairs.add(pair)
        else:
            accepted[pair].add(km)
    output = []
    # Duplicate supplier lines make expected quantities uncertain, even if their sums match.
    lines = Counter((r.get("shipment_id", ""), *variant(r)) for r in rows)
    for row in rows:
        if lines[(row.get("shipment_id", ""), *variant(row))] > 1:
            ambiguous.add((row.get("shipment_id", ""), gtin_key(row.get("gtin", ""))))
    for pair in sorted(demand.keys() | accepted.keys() | invalid_pairs):
        q, count = demand.get(pair, 0), len(accepted[pair])
        status = "source_invalid" if pair in ambiguous else "code_invalid" if pair in invalid_pairs else "orphan" if pair not in demand else "match" if count == q else "shortage" if count < q else "excess"
        output.append({"shipment_id": pair[0], "gtin": pair[1], "quantity": q, "codes": count,
                       "difference": "" if pair in ambiguous else count - q, "status": status})
    return output, issues


def csv_bytes(rows: list[dict], fields: list[str] | None = None, excel_safe: bool = True) -> bytes:
    fields = fields or list(dict.fromkeys(k for row in rows for k in row)) or ["status"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";", extrasaction="ignore")
    def safe(value):
        if excel_safe and isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    # Source column headers are also untrusted spreadsheet cells.
    writer.writerow({f: safe(f) for f in fields})
    for row in rows:
        values = {}
        for field_name in fields:
            value = row.get(field_name, "")
            values[field_name] = safe(value)
        writer.writerow(values)
    return stream.getvalue().encode("utf-8-sig")


def report_zip(result: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("result.json", json.dumps(result, ensure_ascii=False, indent=2))
        for name, records in result["reports"].items():
            archive.writestr(name + ".csv", csv_bytes(records))
        archive.writestr("READ_ME.txt", "Это черновики для проверки, не официальный шаблон 1С/ИС МПТ.\n"
                         "CSV в Excel открывайте через Данные → Из текста/CSV; идентификаторы задавайте как Текст.\n"
                         "Строки с формульным префиксом в CSV защищены апострофом. Исходные строки сохранены в result.json.\n"
                         "GTIN проверен по структуре и контрольной цифре, регистрация не проверена.\n"
                         "NTIN и КМ не проверяются на действительность в реестре. Совпадение количества КМ не подтверждает их подлинность.\n"
                         "Сверка бренда использует только загруженный эталон; его официальный статус подтверждает оператор.\n"
                         "draft_ready означает отсутствие блокирующих ошибок по настроенным правилам; предупреждения нужно просмотреть.\n")
    return buffer.getvalue()


def photo_archive(manifest: Table, data: bytes) -> tuple[bytes, list[dict]]:
    if not {"original_name", "sku", "color", "size", "view"}.issubset(manifest.headers):
        raise ValueError("Манифест фото: нужны original_name, sku, color, size, view.")
    if len(data) > MAX_FILE:
        raise ValueError("ZIP фотографий больше 15 МБ.")
    if not manifest.rows:
        raise ValueError("Таблица соответствий фотографий пуста.")
    output = io.BytesIO()
    audit, used = [], set()

    def component(text):
        cleaned = unicodedata.normalize("NFC", text).strip()
        if not re.fullmatch(r"[\w-]{1,80}", cleaned, re.UNICODE) or cleaned in {".", ".."}:
            raise ValueError("Для имени фото допустимы буквы, цифры, _ и -, до 80 символов в каждом поле.")
        return cleaned

    with validate_archive(data) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        names = Counter(source.namelist())
        selected_sources = set()
        for row in manifest.rows:
            original = row["original_name"]
            path = PurePosixPath(original)
            if path.is_absolute() or ".." in path.parts or "\\" in original or names[original] != 1:
                raise ValueError(f"Фото не найдено однозначно или неверный путь: {original}")
            if original in selected_sources:
                raise ValueError(f"Один исходный файл повторён в манифесте: {original}")
            if manifest.cell_issues.get(int(row["_row"])):
                raise ValueError("В манифесте есть формулы или числовые идентификаторы.")
            extension = path.suffix.casefold()
            if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
                raise ValueError("Разрешены только JPG, PNG и WebP. Содержимое фото не изменяется.")
            sku, color, size, view = [component(row.get(k, "")) for k in ("sku", "color", "size", "view")]
            new_name = f"{sku}/{sku}_{color}_{size}_{view}{extension}"
            if new_name.casefold() in used:
                raise ValueError(f"Совпадение выходных имён: {new_name}")
            used.add(new_name.casefold())
            selected_sources.add(original)
            content = source.read(original)
            target.writestr(new_name, content)
            audit.append({"original_name": original, "new_name": new_name,
                          "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        target.writestr("manifest.csv", csv_bytes(audit))
    return output.getvalue(), audit


def estimate_savings(tasks: list[dict], work_days: float = 20) -> dict:
    if not tasks:
        raise ValueError("Добавьте хотя бы одну операцию.")
    before = after = 0.0
    for task in tasks:
        try:
            minutes, fraction = float(task["minutes"]), float(task["fraction"])
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("Время и доля автоматизации должны быть числами.") from exc
        if not 0 <= minutes <= 1440 or not 0 <= fraction <= 1:
            raise ValueError("Время: 0–1440 минут, доля: 0–1.")
        before += minutes
        after += minutes * (1 - fraction)
    if not 0 < before <= 1440 or not 0 < work_days <= 31:
        raise ValueError("Укажите ненулевой рабочий день до 1440 минут и 1–31 рабочих дней.")
    return {"before_minutes": before, "after_minutes": round(after, 2),
            "saved_minutes": round(before - after, 2), "saved_percent": round((before - after) / before * 100, 1),
            "monthly_hours": round((before - after) * work_days / 60, 1),
            "basis": "scenario_assumptions_not_measurements"}
