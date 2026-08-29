import unittest
from unittest.mock import Mock

from qsirch import MAX_PAGE_SIZE, QsirchClient


class QsirchClientSearchTests(unittest.TestCase):
    def setUp(self):
        self.client = QsirchClient("example.test")
        self.response = Mock()
        self.response.json.return_value = {"items": [], "total": 0}
        self.response.raise_for_status.return_value = None
        self.client._request = Mock(return_value=self.response)

    def test_category_uses_a_get_search_expression(self):
        self.client.search("invoice", category="Email", limit=25, offset=5)

        self.client._request.assert_called_once_with(
            "GET",
            "/qsirch/latest/api/search",
            params={
                "q": "invoice category:Email",
                "limit": 25,
                "offset": 5,
                "advanced_mode": "0",
                "store_history": 0,
            },
        )

    def test_search_bounds_the_page_size_and_offset(self):
        self.client.search("invoice", limit=MAX_PAGE_SIZE + 1, offset=-1)

        params = self.client._request.call_args.kwargs["params"]
        self.assertEqual(MAX_PAGE_SIZE, params["limit"])
        self.assertEqual(0, params["offset"])
        self.assertEqual(0, params["store_history"])


if __name__ == "__main__":
    unittest.main()
