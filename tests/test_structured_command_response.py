import json

import pytest

from structured_command_response import StructuredCommandResponse


def test_decodes_literal_command_text_as_json_chunks_arrive():
    text = 'BEGIN_PLAYGROUND_COMMANDS\nwrite /repo/test.txt <<<\n"😀 café"\t\\path\n>>>\nEND_PLAYGROUND_COMMANDS'
    encoded = json.dumps({"command_block": text})
    response = StructuredCommandResponse()

    preview = "".join(response.add(char) for char in encoded)

    assert preview == response.partial_text() == response.finish() == text


@pytest.mark.parametrize("malformed", [
    '{"command_block":"cat /repo/file"',
    '{"command_block":"safe","command_block":"different"}',
    '{"command_block":"safe"} trailing text',
    '{"command_block": ["not the schema"]}',
    '{"command_block": "\\ud800"}',
])
def test_rejects_invalid_or_ambiguous_envelopes(malformed):
    response = StructuredCommandResponse()
    response.add(malformed)
    with pytest.raises(ValueError):
        response.finish()
