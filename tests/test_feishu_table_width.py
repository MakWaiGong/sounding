import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import feishu_doc


class FakeTableClient:
    def __init__(self):
        self.created = []
        self.batches = []
        self.reads = []
        self.row_count = 0
        self.column_count = 0

    def append_children(self, doc_id, children):
        self.created.append((doc_id, children))
        prop = children[0]["table"]["property"]
        self.row_count = prop["row_size"]
        self.column_count = prop["column_size"]
        return [{"block_id": "table"}]

    def batch_update_blocks(self, doc_id, updates):
        self.batches.append((doc_id, updates))
        for update in updates:
            if "insert_table_row" in update:
                self.row_count += 1

    def all_blocks(self, doc_id):
        self.reads.append(doc_id)
        blocks = []
        for row in range(self.row_count):
            for col in range(self.column_count):
                cell = "cell-%d-%d" % (row, col)
                blocks.append({"parent_id": "table", "block_id": cell})
                blocks.append({"parent_id": cell, "block_id": "text-%d-%d" % (row, col)})
        return blocks


def element_text(elements):
    return "".join(element["text_run"]["content"] if "text_run" in element
                   else "$" + element["equation"]["content"] + "$"
                   for element in elements)


class TableWidthTests(unittest.TestCase):
    def test_explicit_even_widths_for_one_through_nine_columns(self):
        expected_widths = {
            1: [1020],
            2: [510, 510],
            3: [340, 340, 340],
            4: [255, 255, 255, 255],
            5: [204, 204, 204, 204, 204],
            6: [170, 170, 170, 170, 170, 170],
            7: [146, 146, 146, 146, 146, 145, 145],
            8: [128, 128, 128, 128, 127, 127, 127, 127],
            9: [114, 114, 114, 113, 113, 113, 113, 113, 113],
        }
        for width, expected in expected_widths.items():
            with self.subTest(width=width):
                results = []
                for _ in range(2):
                    client = FakeTableClient()
                    table_id = feishu_doc.append_table(client, "doc", [["h"] * width], width)
                    self.assertEqual(table_id, "table")
                    self.assertEqual(len(client.created), 1)
                    doc_id, children = client.created[0]
                    self.assertEqual(doc_id, "doc")
                    self.assertEqual(children[0]["block_type"], 31)
                    prop = children[0]["table"]["property"]
                    self.assertEqual((prop["row_size"], prop["column_size"], prop["header_row"]),
                                     (1, width, True))
                    results.append(prop["column_width"])
                self.assertEqual(results, [expected, expected])
                self.assertTrue(all(type(value) is int and value >= 50 for value in results[0]))
                self.assertEqual(sum(results[0]), 1020)
                self.assertLessEqual(max(results[0]) - min(results[0]), 1)

    def test_large_markdown_table_preserves_headers_cells_and_equations(self):
        rows = [["Symbol", "Meaning", "Formula"],
                ["$x_1$", r"weight $\|W_1\|$", r"epsilon $\varepsilon$"]]
        rows.extend([[f"s{i}", "" if i == 5 else f"label {i}", f"value $x_{i}$"]
                     for i in range(10)])
        lines = ["| " + " | ".join(rows[0]) + " |", "| --- | --- | --- |"]
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        client = FakeTableClient()

        self.assertIs(feishu_doc.append_md(client, "doc", "\n".join(lines)), client)

        prop = client.created[0][1][0]["table"]["property"]
        self.assertEqual(prop, {"row_size": 9, "column_size": 3, "header_row": True,
                                "column_width": [340, 340, 340]})
        self.assertEqual(client.reads, ["doc"])
        self.assertEqual(client.batches[:-1], [
            ("doc", [{"block_id": "table", "insert_table_row": {"row_index": 9, "row_size": 1}}])
        ] * 3)
        doc_id, updates = client.batches[-1]
        self.assertEqual(doc_id, "doc")
        by_id = {item["block_id"]: item["update_text_elements"]["elements"] for item in updates}
        expected = {"text-%d-%d" % (r, c): text
                    for r, row in enumerate(rows) for c, text in enumerate(row) if text}
        self.assertEqual({key: element_text(value) for key, value in by_id.items()}, expected)
        self.assertEqual(len(updates), len(expected))
        self.assertEqual(by_id["text-1-1"], [
            {"text_run": {"content": "weight "}}, {"equation": {"content": r"\|W_1\|"}}])
        self.assertEqual(by_id["text-1-2"], [
            {"text_run": {"content": "epsilon "}}, {"equation": {"content": r"\varepsilon"}}])

    def test_short_rows_keep_their_column_positions(self):
        client = FakeTableClient()
        feishu_doc.append_table(client, "doc", [["a", "b", "c"], ["x"], ["y", "z"]], 3)
        updates = client.batches[-1][1]
        self.assertEqual({item["block_id"]: element_text(item["update_text_elements"]["elements"])
                          for item in updates},
                         {"text-0-0": "a", "text-0-1": "b", "text-0-2": "c",
                          "text-1-0": "x", "text-2-0": "y", "text-2-1": "z"})

    def test_wider_tables_keep_all_columns_and_minimum_width(self):
        for width, expected in ((10, [102] * 10), (21, [50] * 21)):
            with self.subTest(width=width):
                client = FakeTableClient()
                feishu_doc.append_table(client, "doc", [["h"] * width], width)
                prop = client.created[0][1][0]["table"]["property"]
                self.assertEqual(prop["column_size"], width)
                self.assertEqual(prop["column_width"], expected)
                self.assertEqual(len(client.batches[-1][1]), width)

    def test_invalid_dimensions_fail_before_creating_a_block(self):
        cases = [([], 3), ([["a"]], 0), ([["a"]], -1), ([["a"]], None),
                 ([["a"]], 1.0), ([["a"]], True), ([["a"]], "1"),
                 ([["a", "b"]], 1)]
        for rows, width in cases:
            with self.subTest(rows=rows, width=width):
                client = FakeTableClient()
                with self.assertRaises(ValueError):
                    feishu_doc.append_table(client, "doc", rows, width)
                self.assertEqual((client.created, client.batches, client.reads), ([], [], []))


if __name__ == "__main__":
    unittest.main()
