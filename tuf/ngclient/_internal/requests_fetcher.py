# Copyright 2021, New York University and the TUF contributors
# SPDX-License-Identifier: MIT OR Apache-2.0

"""Provides an implementation of FetcherInterface using the Requests HTTP
  library.
"""

import logging
import time
from typing import Iterator, Optional
from urllib import parse

# Imports
import requests
import urllib3.exceptions

import tuf
from tuf import exceptions
from tuf.ngclient.fetcher import FetcherInterface

# Globals
logger = logging.getLogger(__name__)

# Classes
class RequestsFetcher(FetcherInterface):
    """A concrete implementation of FetcherInterface based on the Requests
    library.

    Attributes:
        _sessions: A dictionary of Requests.Session objects storing a separate
            session per scheme+hostname combination.
    """

    def __init__(self):
        # http://docs.python-requests.org/en/master/user/advanced/#session-objects:
        #
        # "The Session object allows you to persist certain parameters across
        # requests. It also persists cookies across all requests made from the
        # Session instance, and will use urllib3's connection pooling. So if
        # you're making several requests to the same host, the underlying TCP
        # connection will be reused, which can result in a significant
        # performance increase (see HTTP persistent connection)."
        #
        # NOTE: We use a separate requests.Session per scheme+hostname
        # combination, in order to reuse connections to the same hostname to
        # improve efficiency, but avoiding sharing state between different
        # hosts-scheme combinations to minimize subtle security issues.
        # Some cookies may not be HTTP-safe.
        self._sessions = {}

        # Default settings
        self.socket_timeout: int = 4  # seconds
        self.chunk_size: int = 400000  # bytes
        self.sleep_before_round: Optional[int] = None

    def fetch(self, url: str, required_length: int) -> Iterator[bytes]:
        """Fetches the contents of HTTP/HTTPS url from a remote server.

        Ensures the length of the downloaded data is up to 'required_length'.

        Arguments:
            url: A URL string that represents a file location.
            required_length: An integer value representing the file length in
                bytes.

        Raises:
            exceptions.SlowRetrievalError: A timeout occurs while receiving
                data.
            exceptions.FetcherHTTPError: An HTTP error code is received.

        Returns:
            A bytes iterator
        """
        # Get a customized session for each new schema+hostname combination.
        session = self._get_session(url)

        # Get the requests.Response object for this URL.
        #
        # Defer downloading the response body with stream=True.
        # Always set the timeout. This timeout value is interpreted by
        # requests as:
        #  - connect timeout (max delay before first byte is received)
        #  - read (gap) timeout (max delay between bytes received)
        response = session.get(url, stream=True, timeout=self.socket_timeout)
        # Check response status.
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            response.close()
            status = e.response.status_code
            raise exceptions.FetcherHTTPError(str(e), status)

        return self._chunks(response, required_length)

    def _chunks(
        self, response: "requests.Response", required_length: int
    ) -> Iterator[bytes]:
        """A generator function to be returned by fetch. This way the
        caller of fetch can differentiate between connection and actual data
        download."""

        try:
            bytes_received = 0
            while True:
                # We download a fixed chunk of data in every round. This is
                # so that we can defend against slow retrieval attacks.
                # Furthermore, we do not wish to download an extremely
                # large file in one shot. Before beginning the round, sleep
                # (if set) for a short amount of time so that the CPU is not
                # hogged in the while loop.
                if self.sleep_before_round:
                    time.sleep(self.sleep_before_round)

                read_amount = min(
                    self.chunk_size,
                    required_length - bytes_received,
                )

                # NOTE: This may not handle some servers adding a
                # Content-Encoding header, which may cause urllib3 to
                #  misbehave:
                # https://github.com/pypa/pip/blob/404838abcca467648180b358598c597b74d568c9/src/pip/_internal/download.py#L547-L582
                data = response.raw.read(read_amount)
                bytes_received += len(data)

                # We might have no more data to read. Check number of bytes
                # downloaded.
                if not data:
                    # Finally, we signal that the download is complete.
                    break

                yield data

                if bytes_received >= required_length:
                    break

            logger.debug(
                "Downloaded %d out of %d bytes",
                bytes_received,
                required_length,
            )

        except urllib3.exceptions.ReadTimeoutError as e:
            raise exceptions.SlowRetrievalError(str(e))

        finally:
            response.close()

    def _get_session(self, url):
        """Returns a different customized requests.Session per schema+hostname
        combination.
        """
        # Use a different requests.Session per schema+hostname combination, to
        # reuse connections while minimizing subtle security issues.
        parsed_url = parse.urlparse(url)

        if not parsed_url.scheme or not parsed_url.hostname:
            raise exceptions.URLParsingError(
                "Could not get scheme and hostname from URL: " + url
            )

        session_index = parsed_url.scheme + "+" + parsed_url.hostname
        session = self._sessions.get(session_index)

        if not session:
            session = requests.Session()
            self._sessions[session_index] = session

            # Attach some default headers to every Session.
            requests_user_agent = session.headers["User-Agent"]
            # Follows the RFC: https://tools.ietf.org/html/rfc7231#section-5.5.3
            tuf_user_agent = (
                "tuf/" + tuf.__version__ + " " + requests_user_agent
            )
            session.headers.update(
                {
                    # Tell the server not to compress or modify anything.
                    # https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Accept-Encoding#Directives
                    "Accept-Encoding": "identity",
                    # The TUF user agent.
                    "User-Agent": tuf_user_agent,
                }
            )

            logger.debug("Made new session %s", session_index)

        else:
            logger.debug("Reusing session %s", session_index)

        return session
