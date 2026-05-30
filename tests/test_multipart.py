"""Multipart body construction for QbtSession.post_multipart."""

from anirss_lib.qbt.session import _build_multipart_body


def test_multipart_body_contains_each_field_and_the_file():
    body = _build_multipart_body(
        boundary="ANIRSSTEST",
        fields={"savepath": "/home/me/Downloads/Anime/Frieren"},
        file_field="torrents",
        file_name="frieren.torrent",
        file_bytes=b"d8:announceXXXe",
        file_content_type="application/x-bittorrent",
    )
    s = body.decode("latin-1")
    # Field part appears with the correct disposition.
    assert "--ANIRSSTEST\r\n" in s
    assert 'Content-Disposition: form-data; name="savepath"' in s
    assert "/home/me/Downloads/Anime/Frieren" in s
    # File part appears with filename and content-type.
    assert 'Content-Disposition: form-data; name="torrents"; filename="frieren.torrent"' in s
    assert "Content-Type: application/x-bittorrent" in s
    assert "d8:announceXXXe" in s
    # Closing boundary is present.
    assert s.endswith("--ANIRSSTEST--\r\n")


def test_multipart_body_preserves_binary_payload():
    """File bytes survive intact even with NULs and high bytes — multipart
    doesn't encode the payload, so a real .torrent file must round-trip."""
    payload = bytes(range(256))  # every byte value 0..255
    body = _build_multipart_body(
        boundary="b",
        fields={},
        file_field="torrents",
        file_name="x.torrent",
        file_bytes=payload,
        file_content_type="application/x-bittorrent",
    )
    # The payload bytes must appear verbatim inside the body.
    assert payload in body


def test_multipart_body_no_fields_just_file():
    body = _build_multipart_body(
        boundary="B",
        fields={},
        file_field="torrents",
        file_name="x.torrent",
        file_bytes=b"abc",
        file_content_type="application/x-bittorrent",
    )
    s = body.decode("latin-1")
    # No savepath part — only the file part + closing boundary.
    assert 'name="savepath"' not in s
    assert 'name="torrents"' in s
    assert s.endswith("--B--\r\n")
