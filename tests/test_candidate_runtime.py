from pathlib import Path


def test_load_candidate_runtime_profile_defaults_when_master_resume_missing():
    from candidate_runtime import load_candidate_runtime_profile

    profile = load_candidate_runtime_profile(Path("/tmp/does-not-exist-master-resume.md"))

    assert profile.full_name == "Candidate Name"
    assert profile.full_name_upper == "CANDIDATE NAME"
    assert profile.contact_line(include_location=True).startswith("San Francisco, CA  |  candidate@example.com")


def test_load_candidate_runtime_profile_parses_master_resume(tmp_path):
    from candidate_runtime import load_candidate_runtime_profile

    master_resume = tmp_path / "master_resume.md"
    master_resume.write_text(
        "\n".join(
            [
                "# Master Resume",
                "",
                "TAYLOR CANDIDATE",
                "New York, NY  |  taylor@example.com  |  555-0101  |  linkedin.com/in/taylor-candidate/  |  taylor.example.com",
                "",
                "## Example Corp — Product Manager",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    profile = load_candidate_runtime_profile(master_resume)

    assert profile.full_name == "Taylor Candidate"
    assert profile.full_name_upper == "TAYLOR CANDIDATE"
    assert profile.contact_line(include_location=False) == (
        "taylor@example.com  |  555-0101  |  linkedin.com/in/taylor-candidate/  |  taylor.example.com"
    )


def test_document_filename_uses_loaded_candidate_name(tmp_path):
    from candidate_runtime import document_filename

    master_resume = tmp_path / "master_resume.md"
    master_resume.write_text(
        "\n".join(
            [
                "# Master Resume",
                "",
                "TAYLOR CANDIDATE",
                "New York, NY  |  taylor@example.com  |  555-0101  |  linkedin.com/in/taylor-candidate/  |  taylor.example.com",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert document_filename("Resume", "Acme", ".pdf", master_resume_path=master_resume) == "Taylor Candidate Resume - Acme.pdf"
