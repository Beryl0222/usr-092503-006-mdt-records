import unittest

from src.mdt_records import ConfirmationState, validate_case_key


class MdtContractTests(unittest.TestCase):
    def test_case_key(self):
        self.assertEqual(validate_case_key("CASE-0123456789ABCDEF"), "CASE-0123456789ABCDEF")

    def test_case_key_rejects_lowercase(self):
        with self.assertRaises(ValueError):
            validate_case_key("CASE-0123456789abcdef")

    def test_invalidated_value(self):
        self.assertEqual(ConfirmationState.INVALIDATED.value, "invalidated")


if __name__ == "__main__":
    unittest.main()
