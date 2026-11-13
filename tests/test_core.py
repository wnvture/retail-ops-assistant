import base64
import csv
import io
import json
import unittest
import zipfile

from retail_ops.core import (Table, analyze, csv_bytes, estimate_savings, gtin_valid,
                             photo_archive, positive_quantity, read_table, report_zip)
from retail_ops.server import demo_payload, run_payload


def table(rows, source="supply.csv"):
    return read_table(source, csv_bytes(rows, excel_safe=False))


def product(**changes):
    row = dict(shipment_id="S001", supplier="Supplier", sku="000001", brand="Demo", name="Shoe",
               color="black", size="42", quantity="2", gtin="012345678905", ntin="DEMO-NTIN")
    row.update(changes)
    return row


class GtinTests(unittest.TestCase):
    def test_known_valid_formats(self):
        for value in ("96385074", "012345678905", "4006381333931", "04006381333931"):
            with self.subTest(value=value):
                self.assertTrue(gtin_valid(value))

    def test_invalid_digit_length_and_unicode(self):
        for value in ("4006381333932", "123", "00000000", "１２３４５６７８", "4E+12", "4006381333931 ", ""):
            with self.subTest(value=value):
                self.assertFalse(gtin_valid(value))

    def test_quantity_boundaries(self):
        for value in ("0", "-1", "1.5", "nan", "Infinity", "10000001", ""):
            with self.subTest(value=value):
                self.assertIsNone(positive_quantity(value))
        self.assertEqual(positive_quantity("2,0"), 2)
        self.assertEqual(positive_quantity("10000000"), 10_000_000)


class ImportTests(unittest.TestCase):
    def test_russian_headers_cp1251_and_leading_zeros(self):
        t = read_table("a.csv", "Артикул;GTIN\n000001;012345678905\n".encode("cp1251"))
        self.assertEqual(t.rows[0]["sku"], "000001")
        self.assertEqual(t.rows[0]["gtin"], "012345678905")

    def test_duplicate_canonical_headers_rejected(self):
        with self.assertRaisesRegex(ValueError, "заголовки"):
            read_table("a.csv", b"sku;SKU\n1;2")

    def test_bad_column_count_rejected(self):
        with self.assertRaisesRegex(ValueError, "число значений"):
            read_table("a.csv", b"sku;gtin\n1;2;3")

    def test_empty_and_wrong_format_rejected(self):
        for name, data in (("empty.csv", b""), ("legacy.xls", b"data")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_table(name, data)

    def test_custom_aliases_extend_defaults(self):
        payload = {"supplies": [{"name": "one.csv", "content": base64.b64encode(csv_bytes([product()], excel_safe=False).replace(b"sku", b"vendor_code")).decode()}], "aliases": {"sku": ["vendor_code"]}}
        result = run_payload(payload)
        self.assertEqual(result["summary"]["draft_ready"], 1)

    def test_unknown_alias_field_rejected(self):
        payload = demo_payload()
        payload["aliases"] = {"anything": ["foo"]}
        with self.assertRaisesRegex(ValueError, "Неизвестное"):
            run_payload(payload)

    def test_formula_prefix_csv_export_protected_json_preserved(self):
        row = {"name": " =HYPERLINK(1)", "sku": "@test", "quantity": -1}
        exported = list(csv.DictReader(io.StringIO(csv_bytes([row]).decode("utf-8-sig")), delimiter=";"))[0]
        self.assertTrue(exported["name"].startswith("'"))
        self.assertTrue(exported["sku"].startswith("'"))
        self.assertEqual(row["name"], " =HYPERLINK(1)")

    def test_km_preserved_exactly(self):
        value = " 01012345678905\x1d21abc "
        t = table([dict(shipment_id="s", gtin="012345678905", km=value)])
        self.assertEqual(t.rows[0]["km"], value)

    def test_formula_header_escaped_in_export(self):
        data = csv_bytes([{"=malicious()": "value"}]).decode("utf-8-sig")
        self.assertTrue(data.startswith("'=malicious()"))


class ProductTests(unittest.TestCase):
    def test_missing_fields_block_row(self):
        r = analyze([table([product(name="")])])
        self.assertEqual(r["summary"]["blocked"], 1)
        self.assertFalse(r["reports"]["km_request_drafts"])

    def test_duplicates_across_files_preserved_and_blocked(self):
        r = analyze([table([product()], "a.csv"), table([product()], "b.csv")])
        self.assertEqual(r["summary"]["rows"], 2)
        self.assertEqual(r["summary"]["blocked"], 2)
        self.assertEqual(sum(i["code"] == "DUPLICATE_LINE" for i in r["reports"]["issues"]), 2)

    def test_same_product_in_different_shipments_allowed(self):
        r = analyze([table([product(), product(shipment_id="S002")])])
        self.assertEqual(r["summary"]["draft_ready"], 2)

    def test_gtin_padded_equivalence_conflict_detected(self):
        r = analyze([table([product(), product(sku="000002", size="43", gtin="00012345678905")])])
        self.assertEqual(r["summary"]["blocked"], 2)
        self.assertTrue(any(i["code"] == "GTIN_CONFLICT" for i in r["reports"]["issues"]))

    def test_same_variant_different_gtin_blocks(self):
        r = analyze([table([product(), product(shipment_id="S002", gtin="4006381333931")])])
        self.assertTrue(all(p["_status"] == "blocked" for p in r["reports"]["products"]))

    def test_ntin_required_only_by_setting(self):
        self.assertEqual(analyze([table([product(ntin="")])])["summary"]["blocked"], 0)
        self.assertEqual(analyze([table([product(ntin="")])], require_ntin=True)["summary"]["blocked"], 1)

    def test_no_reference_not_claimed_checked(self):
        r = analyze([table([product()])])
        self.assertFalse(r["summary"]["reference_supplied"])
        self.assertTrue(any(i["code"] == "REFERENCE_NOT_SUPPLIED" for i in r["reports"]["issues"]))

    def test_reference_difference_does_not_overwrite(self):
        ref = table([product(name="Other shoe", source_url="https://example.com/a", checked_at="2026-11-13")], "ref.csv")
        r = analyze([table([product()])], reference=ref)
        self.assertEqual(r["summary"]["blocked"], 1)
        self.assertEqual(r["reports"]["products"][0]["name"], "Shoe")
        self.assertEqual(r["reports"]["reference_comparison"][0]["expected"], "Other shoe")

    def test_reference_ambiguity_never_arbitrary_first_match(self):
        p = product(source_url="https://example.com/a", checked_at="2026-11-13")
        r = analyze([table([product()])], reference=table([p, {**p, "name": "Different"}], "ref.csv"))
        self.assertFalse(r["reports"]["reference_comparison"])
        self.assertTrue(any(i["code"] == "REFERENCE_AMBIGUOUS" for i in r["reports"]["issues"]))

    def test_reference_without_url_blocked(self):
        ref = table([product(source_url="", checked_at="2026-11-13")], "ref.csv")
        self.assertEqual(analyze([table([product()])], reference=ref)["summary"]["blocked"], 1)

    def test_unsafe_excel_cells_blocked(self):
        t = table([product()])
        t.cell_issues = {2: [("sku", "Numeric identifier")]}
        self.assertEqual(analyze([t])["summary"]["blocked"], 1)

    def test_same_source_name_rejected(self):
        with self.assertRaisesRegex(ValueError, "Имена"):
            analyze([table([product()]), table([product()])])

    def test_empty_input_rejected(self):
        with self.assertRaises(ValueError):
            analyze([])


class CodeTests(unittest.TestCase):
    def check_codes(self, entries, products=None):
        return analyze([table(products or [product()])], codes=table(entries, "km.csv"))

    def test_shortage_match_excess(self):
        for count, status in ((1, "shortage"), (2, "match"), (3, "excess")):
            entries = [dict(shipment_id="S001", gtin="012345678905", km="demo-"+str(i)) for i in range(count)]
            with self.subTest(count=count):
                r = self.check_codes(entries)
                self.assertEqual(r["reports"]["km_reconciliation"][0]["status"], status)

    def test_duplicate_kms_are_not_counted(self):
        entry = dict(shipment_id="S001", gtin="012345678905", km="repeat")
        r = self.check_codes([entry, entry])
        row = r["reports"]["km_reconciliation"][0]
        self.assertEqual(row["codes"], 0)
        self.assertEqual(row["status"], "code_invalid")

    def test_cross_shipment_duplicate_code_invalidates_both(self):
        entry = dict(shipment_id="S001", gtin="012345678905", km="repeat")
        r = self.check_codes([entry, {**entry, "shipment_id": "S002"}])
        self.assertEqual(sum(x["status"] == "code_invalid" for x in r["reports"]["km_reconciliation"]), 2)

    def test_orphan_and_missing_file_distinguished(self):
        r = self.check_codes([dict(shipment_id="S999", gtin="012345678905", km="unique")])
        self.assertIn("orphan", [r["status"] for r in r["reports"]["km_reconciliation"]])
        r = analyze([table([product()])])
        self.assertEqual(r["reports"]["km_reconciliation"][0]["status"], "not_checked")

    def test_duplicate_supply_never_reports_clean_match(self):
        entries = [dict(shipment_id="S001", gtin="012345678905", km=str(i)) for i in range(4)]
        r = self.check_codes(entries, [product(), product()])
        self.assertEqual(r["reports"]["km_reconciliation"][0]["status"], "source_invalid")


class PhotoTests(unittest.TestCase):
    def archive(self, names=("photo.jpg",)):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            for name in names:
                z.writestr(name, b"test bytes preserved, no image decoding")
        return buffer.getvalue()

    def manifest(self, **changes):
        return {"original_name": "photo.jpg", "sku": "000001", "color": "black", "size": "42", "view": "front", **changes}

    def test_photo_content_and_audit_preserved(self):
        result, audit = photo_archive(table([self.manifest()]), self.archive())
        with zipfile.ZipFile(io.BytesIO(result)) as z:
            self.assertEqual(z.read("000001/000001_black_42_front.jpg"), b"test bytes preserved, no image decoding")
            self.assertIn("manifest.csv", z.namelist())
        self.assertEqual(audit[0]["original_name"], "photo.jpg")

    def test_target_collision_rejected(self):
        rows = [self.manifest(), self.manifest(original_name="photo2.jpg")]
        with self.assertRaisesRegex(ValueError, "Совпадение"):
            photo_archive(table(rows), self.archive(("photo.jpg", "photo2.jpg")))

    def test_traversal_and_duplicate_source_rejected(self):
        with self.assertRaises(ValueError):
            photo_archive(table([self.manifest(original_name="../photo.jpg")]), self.archive(("../photo.jpg",)))
        with self.assertRaisesRegex(ValueError, "повторён"):
            photo_archive(table([self.manifest(), self.manifest(view="side")]), self.archive())

    def test_unsupported_extension_or_invalid_component_rejected(self):
        for changes, names in (({"original_name": "a.exe"}, ("a.exe",)), ({"sku": "a/b"}, ("photo.jpg",))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                photo_archive(table([self.manifest(**changes)]), self.archive(names))


class EndToEndTests(unittest.TestCase):
    def test_demo_expected_outcome_and_export(self):
        result = run_payload(demo_payload())
        self.assertEqual(result["summary"]["rows"], 8)
        self.assertEqual(result["summary"]["draft_ready"], 3)
        self.assertEqual(result["summary"]["blocked"], 5)
        result.pop("zip_base64")
        with zipfile.ZipFile(io.BytesIO(report_zip(result))) as z:
            restored = json.loads(z.read("result.json"))
            self.assertEqual(restored["summary"], result["summary"])
            self.assertEqual(len(restored["file_sha256"]), 4)
            self.assertIn("product_card_drafts.csv", z.namelist())

    def test_time_scenario_exact_calculation(self):
        r = estimate_savings([{"minutes":m, "fraction":f} for m, f in [(150,.8),(90,.5),(60,.9),(120,.3),(60,0)]])
        self.assertEqual(r["saved_minutes"], 255)
        self.assertEqual(r["saved_percent"], 53.1)
        self.assertEqual(r["monthly_hours"], 85)
        self.assertEqual(r["after_minutes"], 225)

    def test_time_boundaries(self):
        for tasks in ([], [{"minutes": 0, "fraction": 0}], [{"minutes": 10, "fraction": 1.1}], [{"minutes": float("nan"), "fraction": 0}]):
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                estimate_savings(tasks)


if __name__ == "__main__":
    unittest.main()
