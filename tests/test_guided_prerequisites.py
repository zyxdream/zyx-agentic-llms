from backend.util.guided_prerequisites import guided_prerequisite_error


def test_guided_prerequisite_error_rejects_empty_saved_setup_values():
    memory = {
        "temp/consolidated_rfas": "[]",
        "inputs/plaintiff": "Plaintiff",
        "inputs/defendant": "",
        "inputs/venue": "Superior Court",
    }

    result = guided_prerequisite_error(
        memory,
        (
            "temp/consolidated_rfas",
            "inputs/plaintiff",
            "inputs/defendant",
            "inputs/venue",
        ),
        "requests for admission",
    )

    assert result is not None
    assert result["missing_prerequisites"] == [
        "temp/consolidated_rfas",
        "inputs/defendant",
    ]


def test_guided_prerequisite_error_allows_prepared_setup_values():
    memory = {
        "temp/consolidated_rfas": '[{"Admission Request": "Admit the contract exists."}]',
        "inputs/plaintiff": "Plaintiff",
        "inputs/defendant": "Defendant",
        "inputs/venue": "Superior Court",
    }

    result = guided_prerequisite_error(
        memory,
        (
            "temp/consolidated_rfas",
            "inputs/plaintiff",
            "inputs/defendant",
            "inputs/venue",
        ),
        "requests for admission",
    )

    assert result is None
