from __future__ import annotations

import json

import promote
import pytest

SHA = "a" * 40
SUMS = f"{'1' * 64}  ./omnigent-staging-debug.apk\n{'2' * 64}  ./merge-report.json\n"


def test_build_complete_lists_assets_and_digests():
    doc = promote.build_complete(
        "nightly-20261010", SHA, SUMS, "sha256:" + "3" * 64, "sha256:" + "4" * 64
    )
    assert doc == {
        "schema": 1,
        "tag": "nightly-20261010",
        "sha": SHA,
        "assets": {"omnigent-staging-debug.apk": "1" * 64, "merge-report.json": "2" * 64},
        "images": {"server": "sha256:" + "3" * 64, "host": "sha256:" + "4" * 64},
    }


@pytest.mark.parametrize(
    "tag,sha,server",
    [
        ("production-20261010", SHA, "sha256:" + "3" * 64),
        ("nightly-20261010", "xyz", "sha256:" + "3" * 64),
        ("nightly-20261010", SHA, "latest"),
    ],
)
def test_build_complete_rejects_bad_inputs(tag, sha, server):
    with pytest.raises(promote.PromoteError):
        promote.build_complete(tag, sha, SUMS, server, "sha256:" + "4" * 64)


def test_build_complete_rejects_malformed_sums():
    with pytest.raises(promote.PromoteError):
        promote.build_complete(
            "nightly-20261010", SHA, "garbage\n", "sha256:" + "3" * 64, "sha256:" + "4" * 64
        )


def test_build_complete_cli(tmp_path, capsys):
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(SUMS)
    assert (
        promote.main(
            [
                "build-complete",
                "--tag",
                "nightly-20261010",
                "--sha",
                SHA,
                "--sums",
                str(sums),
                "--server-digest",
                "sha256:" + "3" * 64,
                "--host-digest",
                "sha256:" + "4" * 64,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["assets"]["merge-report.json"] == "2" * 64
