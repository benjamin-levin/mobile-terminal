from pathlib import Path
import unittest


APP_JS = Path(__file__).parents[1] / "static" / "app.js"


class ResumeReconnectWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_JS.read_text()

    def test_resume_signals_refresh_the_connection(self):
        expected = (
            'window.addEventListener("focus", () => {',
            'document.addEventListener("visibilitychange", () => {',
            'window.addEventListener("online", refreshConnection);',
            'window.addEventListener("pageshow", (event) => {',
        )
        for marker in expected:
            self.assertTrue(marker in self.source, f"missing resume signal: {marker}")

    def test_closed_socket_reconnects_instead_of_dropping_refresh(self):
        marker = "if (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING)"
        self.assertTrue(marker in self.source, "closed sockets are not reconnected on resume")
        self.assertTrue("reconnectSocket();" in self.source, "reconnect helper is not called")

    def test_ghost_open_socket_has_a_response_timeout(self):
        expected = (
            "const observedMessageAt = lastServerMessageAt;",
            "lastServerMessageAt <= observedMessageAt",
            "}, 4000);",
        )
        for marker in expected:
            self.assertTrue(marker in self.source, f"missing liveness probe marker: {marker}")

    def test_replaced_socket_events_cannot_start_an_extra_reconnect(self):
        self.assertGreaterEqual(
            self.source.count("if (socket !== thisSocket)"),
            2,
            "stale message and close handlers are not both guarded",
        )


if __name__ == "__main__":
    unittest.main()
