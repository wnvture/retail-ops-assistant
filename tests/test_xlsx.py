"""Tiny OOXML test fixtures exercise the real reader without authoring dependencies."""

import importlib.util
import io
import unittest
import zipfile

from retail_ops.core import read_table


def fixture(numeric=False, formula=False):
    content = {
        "[Content_Types].xml": '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>''',
        "_rels/.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''',
        "xl/workbook.xml": '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>''',
    }
    value = '<c r="A2" t="n"><v>12345678905</v></c>' if numeric else '<c r="A2" t="inlineStr"><is><t>012345678905</t></is></c>'
    name = '<c r="B2"><f>1+1</f><v>2</v></c>' if formula else '<c r="B2" t="inlineStr"><is><t>Shoe</t></is></c>'
    content["xl/worksheets/sheet1.xml"] = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:B2"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>GTIN</t></is></c><c r="B1" t="inlineStr"><is><t>name</t></is></c></row><row r="2">'+value+name+'</row></sheetData></worksheet>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, text in content.items():
            z.writestr(name, text)
    return buffer.getvalue()


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional Excel reader not installed")
class XlsxReaderTests(unittest.TestCase):
    def test_text_leading_zero_survives_real_reader(self):
        t = read_table("test.xlsx", fixture())
        self.assertEqual(t.rows[0]["gtin"], "012345678905")
        self.assertFalse(t.cell_issues)

    def test_numeric_identifier_is_explicit_issue(self):
        t = read_table("test.xlsx", fixture(numeric=True))
        self.assertEqual(t.cell_issues[2][0][0], "gtin")

    def test_formula_not_used_as_cached_value(self):
        t = read_table("test.xlsx", fixture(formula=True))
        self.assertEqual(t.rows[0]["name"], "=1+1")
        self.assertEqual(t.cell_issues[2][0][0], "name")
