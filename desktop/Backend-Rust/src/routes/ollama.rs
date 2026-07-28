// Gemini <-> self-hosted LLM translation.
//
// The desktop app speaks the Gemini REST shape (generateContent /
// streamGenerateContent / embedContent / batchEmbedContents) to our proxy.
// This module translates it to the self-hosted backend so the app stays
// unchanged. Enabled when OLLAMA_URL is set (see proxy.rs dispatch).
//
// Routing (single scheduler endpoint for generation):
//   - generateContent -> OLLAMA_URL + `/v1/chat/completions` (OpenAI-compatible).
//     This targets the GPU scheduler's real model set (e.g. gemma4-it-e4b-FLM on
//     the NPU) — the same endpoint the interactive chat path uses. The native
//     `/api/chat` route reaches only raw ollama's small model set, so we avoid it.
//   - embedContent/batchEmbedContents -> OLLAMA_EMBED_URL + `/api/embeddings`
//     (ollama-native), served by the dedicated nomic-embed-text pod.

use std::time::Duration;

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::{json, Value};

const OLLAMA_TIMEOUT: Duration = Duration::from_secs(180);

/// Concatenate the text of a Gemini `parts` array (non-text parts are ignored).
fn parts_text(parts: &Value) -> String {
    parts
        .as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                .collect::<Vec<_>>()
                .join("")
        })
        .unwrap_or_default()
}

/// Map a Gemini role to an Ollama (OpenAI-style) role.
fn map_role(role: &str) -> &'static str {
    match role {
        "model" => "assistant",
        "system" => "system",
        _ => "user",
    }
}

/// Translate a Gemini generateContent body into an OpenAI /v1/chat/completions request.
pub fn gemini_to_openai_chat(body: &[u8], model: &str, stream: bool) -> Result<Value, String> {
    let g: Value = serde_json::from_slice(body).map_err(|e| format!("bad gemini json: {e}"))?;
    let mut messages: Vec<Value> = Vec::new();

    // system_instruction / systemInstruction -> a leading system message
    if let Some(sys) = g.get("systemInstruction").or_else(|| g.get("system_instruction")) {
        let text = parts_text(sys.get("parts").unwrap_or(&Value::Null));
        if !text.is_empty() {
            messages.push(json!({"role": "system", "content": text}));
        }
    }

    if let Some(contents) = g.get("contents").and_then(|c| c.as_array()) {
        for c in contents {
            let role = c.get("role").and_then(|r| r.as_str()).unwrap_or("user");
            let text = parts_text(c.get("parts").unwrap_or(&Value::Null));
            messages.push(json!({"role": map_role(role), "content": text}));
        }
    }

    let mut req = json!({ "model": model, "messages": messages, "stream": stream });
    // generationConfig -> top-level OpenAI sampling params.
    if let Some(gc) = g.get("generationConfig").or_else(|| g.get("generation_config")) {
        if let Some(t) = gc.get("temperature") {
            req["temperature"] = t.clone();
        }
        if let Some(n) = gc.get("maxOutputTokens").or_else(|| gc.get("max_output_tokens")) {
            req["max_tokens"] = n.clone();
        }
        if let Some(p) = gc.get("topP").or_else(|| gc.get("top_p")) {
            req["top_p"] = p.clone();
        }
    }
    Ok(req)
}

/// Translate an OpenAI /v1/chat/completions (non-stream) response into a Gemini response.
pub fn openai_chat_to_gemini(openai: &Value) -> Value {
    let text = openai
        .get("choices")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("message"))
        .and_then(|m| m.get("content"))
        .and_then(|c| c.as_str())
        .unwrap_or("");
    let usage = openai.get("usage");
    let prompt = usage.and_then(|u| u.get("prompt_tokens")).and_then(|v| v.as_i64()).unwrap_or(0);
    let completion = usage.and_then(|u| u.get("completion_tokens")).and_then(|v| v.as_i64()).unwrap_or(0);
    json!({
        "candidates": [{
            "content": { "parts": [{ "text": text }], "role": "model" },
            "finishReason": "STOP",
            "index": 0
        }],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": completion,
            "totalTokenCount": prompt + completion
        }
    })
}

fn base(url: &str) -> &str {
    url.trim_end_matches('/')
}

/// Handle generateContent (and streamGenerateContent, as a single SSE event) by
/// calling the scheduler's OpenAI-compatible /v1/chat/completions and translating
/// the response back to Gemini shape.
pub async fn handle_generate(
    ollama_url: &str,
    model: &str,
    gemini_body: &[u8],
    as_stream: bool,
) -> Result<Response, StatusCode> {
    let req = gemini_to_openai_chat(gemini_body, model, false).map_err(|e| {
        tracing::warn!("gemini_proxy: gemini->openai translate failed: {e}");
        StatusCode::BAD_REQUEST
    })?;
    let url = format!("{}/v1/chat/completions", base(ollama_url));
    let resp = reqwest::Client::new()
        .post(&url)
        .timeout(OLLAMA_TIMEOUT)
        .json(&req)
        .send()
        .await
        .map_err(|e| {
            tracing::error!("gemini_proxy: /v1/chat/completions request failed: {e}");
            StatusCode::BAD_GATEWAY
        })?;
    if !resp.status().is_success() {
        let code = resp.status().as_u16();
        let txt = resp.text().await.unwrap_or_default();
        tracing::error!("gemini_proxy: /v1/chat/completions upstream {code}: {txt}");
        return Err(StatusCode::from_u16(code).unwrap_or(StatusCode::BAD_GATEWAY));
    }
    let openai_resp: Value = resp.json().await.map_err(|e| {
        tracing::error!("gemini_proxy: bad /v1/chat/completions json: {e}");
        StatusCode::BAD_GATEWAY
    })?;
    let gemini = openai_chat_to_gemini(&openai_resp);

    if as_stream {
        // Emit the full translated response as a single Gemini SSE event.
        // (Real token-by-token streaming is a future enhancement.)
        let body = format!("data: {}\n\n", serde_json::to_string(&gemini).unwrap_or_default());
        Ok(Response::builder()
            .status(StatusCode::OK)
            .header("content-type", "text/event-stream")
            .body(axum::body::Body::from(body))
            .unwrap())
    } else {
        Ok((StatusCode::OK, axum::Json(gemini)).into_response())
    }
}

async fn embed_one(
    client: &reqwest::Client,
    url: &str,
    model: &str,
    text: &str,
) -> Result<Value, StatusCode> {
    let resp = client
        .post(url)
        .timeout(OLLAMA_TIMEOUT)
        .json(&json!({ "model": model, "prompt": text }))
        .send()
        .await
        .map_err(|e| {
            tracing::error!("ollama: /api/embeddings request failed: {e}");
            StatusCode::BAD_GATEWAY
        })?;
    if !resp.status().is_success() {
        let code = resp.status().as_u16();
        tracing::error!("ollama: /api/embeddings upstream {code}");
        return Err(StatusCode::from_u16(code).unwrap_or(StatusCode::BAD_GATEWAY));
    }
    let v: Value = resp.json().await.map_err(|_| StatusCode::BAD_GATEWAY)?;
    Ok(v.get("embedding").cloned().unwrap_or(Value::Array(vec![])))
}

/// Handle embedContent / batchEmbedContents via Ollama /api/embeddings.
pub async fn handle_embed(
    ollama_url: &str,
    model: &str,
    gemini_body: &[u8],
    batch: bool,
) -> Result<Response, StatusCode> {
    let g: Value = serde_json::from_slice(gemini_body).map_err(|_| StatusCode::BAD_REQUEST)?;
    let client = reqwest::Client::new();
    let url = format!("{}/api/embeddings", base(ollama_url));

    if batch {
        let reqs = g.get("requests").and_then(|r| r.as_array()).cloned().unwrap_or_default();
        let mut embeddings = Vec::with_capacity(reqs.len());
        for r in reqs {
            let text = parts_text(r.get("content").and_then(|c| c.get("parts")).unwrap_or(&Value::Null));
            let values = embed_one(&client, &url, model, &text).await?;
            embeddings.push(json!({ "values": values }));
        }
        Ok((StatusCode::OK, axum::Json(json!({ "embeddings": embeddings }))).into_response())
    } else {
        let text = parts_text(g.get("content").and_then(|c| c.get("parts")).unwrap_or(&Value::Null));
        let values = embed_one(&client, &url, model, &text).await?;
        Ok((StatusCode::OK, axum::Json(json!({ "embedding": { "values": values } }))).into_response())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn translates_contents_and_system_to_messages() {
        let body = br#"{
            "systemInstruction": {"parts": [{"text": "be brief"}]},
            "contents": [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "hello"}]}
            ],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 128}
        }"#;
        let out = gemini_to_openai_chat(body, "m", false).unwrap();
        assert_eq!(out["model"], "m");
        assert_eq!(out["stream"], false);
        let msgs = out["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 3);
        assert_eq!(msgs[0]["role"], "system");
        assert_eq!(msgs[1]["role"], "user");
        assert_eq!(msgs[2]["role"], "assistant"); // gemini "model" -> openai "assistant"
        // OpenAI sampling params are top-level (not nested under `options`).
        assert_eq!(out["max_tokens"], 128);
        assert_eq!(out["temperature"], 0.5);
    }

    #[test]
    fn translates_openai_response_to_gemini() {
        let openai = json!({
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "the answer"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        });
        let g = openai_chat_to_gemini(&openai);
        assert_eq!(g["candidates"][0]["content"]["parts"][0]["text"], "the answer");
        assert_eq!(g["candidates"][0]["content"]["role"], "model");
        assert_eq!(g["candidates"][0]["finishReason"], "STOP");
        assert_eq!(g["usageMetadata"]["totalTokenCount"], 13);
    }
}
