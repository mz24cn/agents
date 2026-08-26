import json

from runtime.models import Message, ModelConfig, ToolConfig
from runtime.protocols import OpenAIResponsesProtocol, PROTOCOL_MAP


def config(**params):
    return ModelConfig(
        model_id="responses-test",
        api_base="https://api.openai.com/v1",
        api_key="secret",
        model_name="gpt-5",
        api_protocol="responses",
        generate_params=params,
    )


def test_build_request_with_reasoning_tools_and_history():
    protocol = OpenAIResponsesProtocol()
    tools = [ToolConfig(
        tool_id="weather", tool_type="function", name="weather", description="Weather",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
    )]
    messages = [
        Message(role="system", content="Be useful"),
        Message(role="user", content="Weather?"),
        Message(role="assistant", content="", tool_calls=[{
            "id": "call_1", "name": "weather", "arguments": '{"city":"Paris"}'
        }]),
        Message(role="tool", tool_use_id="call_1", content='{"temp":20}'),
    ]
    url, headers, raw = protocol.build_request(
        config(reasoning={"effort": "medium"}, max_completion_tokens=100),
        messages, tools, False,
    )
    body = json.loads(raw)
    assert url == "https://api.openai.com/v1/responses"
    assert headers["Authorization"] == "Bearer secret"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["max_output_tokens"] == 100
    assert "max_completion_tokens" not in body
    assert body["tools"][0]["name"] == "weather"
    assert "function" not in body["tools"][0]
    assert body["input"][2]["type"] == "function_call"
    assert body["input"][3] == {
        "type": "function_call_output", "call_id": "call_1", "output": '{"temp":20}'
    }


def test_parse_non_stream_response():
    payload = {
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Consider"}]},
            {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Hello"}
            ]},
            {"type": "function_call", "call_id": "call_2", "name": "lookup",
             "arguments": '{"q":"x"}'},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
    messages, usage = OpenAIResponsesProtocol().parse_response(json.dumps(payload).encode())
    assert messages[0].content == "Hello"
    assert messages[0].thinking == "Consider"
    assert messages[0].tool_calls == [{
        "id": "call_2", "name": "lookup", "arguments": '{"q":"x"}'
    }]
    assert usage.total_tokens == 15


def test_parse_stream_and_registry():
    raw = b"\n".join([
        b'data: {"type":"response.output_text.delta","delta":"Hi"}',
        b'data: {"type":"response.reasoning_summary_text.delta","delta":"Think"}',
        b'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"c","name":"f","arguments":"{}"}}',
        b'data: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":3,"total_tokens":5}}}',
        b'data: [DONE]',
    ])
    messages, usage = OpenAIResponsesProtocol().parse_response(raw, stream=True)
    assert messages[0].content == "Hi"
    assert messages[0].thinking == "Think"
    assert messages[0].tool_calls[0]["id"] == "c"
    assert usage.total_tokens == 5
    assert PROTOCOL_MAP["responses"] is OpenAIResponsesProtocol
