"""Block Python network access. Native libraries and external tools need separate checks."""
import sys

_installed = False


def _guard(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.bind", "socket.sendto", "socket.sendmsg"}:
        sock = args[0]
        # AF_UNIX is needed by local multiprocessing and native integrations.
        if int(sock.family) != 1:
            raise PermissionError("ENG Drawing Viewer blocks network sockets.")
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                 "urllib.Request", "http.client.connect", "webbrowser.open"}:
        raise PermissionError("ENG Drawing Viewer works offline.")


def install():
    global _installed
    if not _installed:
        sys.addaudithook(_guard)
        _installed = True
