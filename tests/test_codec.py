import unittest

from serial_monitor import codec


class ParseHexTests(unittest.TestCase):
    def test_space_separated(self):
        self.assertEqual(codec.parse_hex("01 03 00 0A ff"), b"\x01\x03\x00\x0a\xff")

    def test_various_separators_and_prefixes(self):
        self.assertEqual(codec.parse_hex("0x01,0x02;03:04-05"), b"\x01\x02\x03\x04\x05")
        self.assertEqual(codec.parse_hex("AAh BBh"), b"\xaa\xbb")

    def test_contiguous(self):
        self.assertEqual(codec.parse_hex("DEADBEEF"), b"\xde\xad\xbe\xef")

    def test_single_nibble(self):
        self.assertEqual(codec.parse_hex("1 2 A"), b"\x01\x02\x0a")

    def test_empty(self):
        self.assertEqual(codec.parse_hex("   "), b"")

    def test_invalid(self):
        with self.assertRaises(codec.ParseError):
            codec.parse_hex("01 ZZ")
        with self.assertRaises(codec.ParseError):
            codec.parse_hex("123")


class ParseAsciiTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(codec.parse_ascii("AT+GMR"), b"AT+GMR")

    def test_escapes(self):
        self.assertEqual(codec.parse_ascii(r"A\r\nB\t\x1B\\"), b"A\r\nB\t\x1b\\")

    def test_escapes_disabled(self):
        self.assertEqual(codec.parse_ascii(r"A\r", escapes=False), b"A\\r")

    def test_unknown_escape_kept(self):
        self.assertEqual(codec.parse_ascii(r"\q"), b"\\q")

    def test_trailing_backslash(self):
        self.assertEqual(codec.parse_ascii("abc\\"), b"abc\\")

    def test_bad_hex_escape(self):
        with self.assertRaises(codec.ParseError):
            codec.parse_ascii(r"\xZ1")

    def test_non_latin1(self):
        with self.assertRaises(codec.ParseError):
            codec.parse_ascii("€")


class PayloadTests(unittest.TestCase):
    def test_ascii_with_crlf(self):
        self.assertEqual(codec.build_payload("AT", "ASCII", "CR+LF (\\r\\n)"), b"AT\r\n")

    def test_hex_no_ending(self):
        self.assertEqual(codec.build_payload("01 02", "HEX", "None"), b"\x01\x02")

    def test_hex_with_lf(self):
        self.assertEqual(codec.build_payload("01", "hex", "LF (\\n)"), b"\x01\n")


class FormatTests(unittest.TestCase):
    def test_to_hex(self):
        self.assertEqual(codec.to_hex(b"\x00\x0a\xff"), "00 0A FF")

    def test_to_ascii(self):
        self.assertEqual(codec.to_ascii(b"Hi\x00\r\n"), "Hi.\r\n")
        self.assertEqual(codec.to_ascii(b"Hi\x00\r\n", show_control=True),
                         "Hi<00><CR><LF>\n")

    def test_hex_dump(self):
        dump = codec.hex_dump(b"ABC\x00")
        self.assertTrue(dump.startswith("41 42 43 00"))
        self.assertTrue(dump.endswith("|ABC.|"))
        self.assertEqual(len(codec.hex_dump(bytes(20)).splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
