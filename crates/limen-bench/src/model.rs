//! A thin, vendor-neutral OpenAI-compatible chat client.
//!
//! Works with any endpoint that speaks the OpenAI `/chat/completions` + `/models` API. Endpoint
//! and credentials are read from the environment so **no key or provider detail is committed**:
//!
//! - `INFERENCE_HUB_API_KEY` (required) — sent as `Authorization: Bearer <key>`.
//! - `INFERENCE_HUB_BASE_URL` (required) — e.g. `https://your-endpoint/v1`.
//!
//! Discover the exact model ids your endpoint serves with the `limen-bench models` subcommand.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

/// One chat message in the OpenAI schema.
#[derive(Clone, Debug, Serialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

impl ChatMessage {
    pub fn system(content: impl Into<String>) -> Self {
        Self {
            role: "system".into(),
            content: content.into(),
        }
    }
    pub fn user(content: impl Into<String>) -> Self {
        Self {
            role: "user".into(),
            content: content.into(),
        }
    }
}

/// Sampling parameters. `seed` is forwarded for reproducibility where the backend honors it.
#[derive(Clone, Debug)]
pub struct CompletionParams {
    pub temperature: f32,
    pub max_tokens: u32,
    pub seed: Option<u64>,
}

impl Default for CompletionParams {
    fn default() -> Self {
        Self {
            temperature: 0.2,
            max_tokens: 4096,
            seed: None,
        }
    }
}

/// A thin OpenAI-compatible client (non-streaming).
pub struct ModelClient {
    http: reqwest::Client,
    base_url: String,
    api_key: String,
}

impl ModelClient {
    /// Build from the environment (no secrets in code).
    pub fn from_env() -> Result<Self> {
        let api_key = std::env::var("INFERENCE_HUB_API_KEY").context(
            "set INFERENCE_HUB_API_KEY to your OpenAI-compatible endpoint's bearer token",
        )?;
        let base_url = std::env::var("INFERENCE_HUB_BASE_URL").context(
            "set INFERENCE_HUB_BASE_URL to your endpoint, e.g. https://your-endpoint/v1",
        )?;
        Ok(Self::new(base_url, api_key))
    }

    pub fn new(base_url: String, api_key: String) -> Self {
        Self {
            http: reqwest::Client::new(),
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key,
        }
    }

    /// `GET /models` → available model ids (to discover the exact strings your endpoint serves).
    pub async fn list_models(&self) -> Result<Vec<String>> {
        #[derive(Deserialize)]
        struct Model {
            id: String,
        }
        #[derive(Deserialize)]
        struct Resp {
            data: Vec<Model>,
        }
        let resp: Resp = self
            .http
            .get(format!("{}/models", self.base_url))
            .bearer_auth(&self.api_key)
            .send()
            .await?
            .error_for_status()
            .context("listing models failed")?
            .json()
            .await?;
        Ok(resp.data.into_iter().map(|m| m.id).collect())
    }

    /// `POST /v1/chat/completions` (non-streaming) → the assistant message content.
    pub async fn complete(
        &self,
        model: &str,
        messages: &[ChatMessage],
        params: &CompletionParams,
    ) -> Result<String> {
        #[derive(Deserialize)]
        struct Msg {
            content: String,
        }
        #[derive(Deserialize)]
        struct Choice {
            message: Msg,
        }
        #[derive(Deserialize)]
        struct Resp {
            choices: Vec<Choice>,
        }
        let resp: Resp = self
            .http
            .post(format!("{}/chat/completions", self.base_url))
            .bearer_auth(&self.api_key)
            .json(&self.request_body(model, messages, params))
            .send()
            .await?
            .error_for_status()
            .context("chat/completions request failed")?
            .json()
            .await?;
        resp.choices
            .into_iter()
            .next()
            .map(|c| c.message.content)
            .context("no choices in completion response")
    }

    fn request_body(
        &self,
        model: &str,
        messages: &[ChatMessage],
        params: &CompletionParams,
    ) -> serde_json::Value {
        let mut body = serde_json::json!({
            "model": model,
            "messages": messages,
            "temperature": params.temperature,
            "max_tokens": params.max_tokens,
            "stream": false,
        });
        if let Some(seed) = params.seed {
            body["seed"] = serde_json::json!(seed);
        }
        body
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_body_is_openai_shaped() {
        let client = ModelClient::new("https://api.example.com/v1".into(), "k".into());
        let body = client.request_body(
            "vendor/model-name",
            &[ChatMessage::system("be terse"), ChatMessage::user("hi")],
            &CompletionParams {
                temperature: 0.0,
                max_tokens: 16,
                seed: Some(7),
            },
        );
        assert_eq!(body["model"], "vendor/model-name");
        assert_eq!(body["messages"][0]["role"], "system");
        assert_eq!(body["messages"][1]["content"], "hi");
        assert_eq!(body["stream"], false);
        assert_eq!(body["seed"], 7);
        assert_eq!(body["max_tokens"], 16);
    }

    #[test]
    fn base_url_trailing_slash_is_trimmed() {
        let client = ModelClient::new("https://api.example.com/v1/".into(), "k".into());
        assert_eq!(client.base_url, "https://api.example.com/v1");
    }

    // Live check: needs network + INFERENCE_HUB_API_KEY + INFERENCE_HUB_BASE_URL + LIMEN_BENCH_MODEL.
    //   INFERENCE_HUB_API_KEY=... INFERENCE_HUB_BASE_URL=... LIMEN_BENCH_MODEL=... \
    //     cargo test -p limen-bench -- --ignored live_inference_hub
    #[tokio::test]
    #[ignore = "live network + endpoint env"]
    async fn live_inference_hub_smoke() {
        let client = ModelClient::from_env().unwrap();
        let models = client.list_models().await.unwrap();
        assert!(!models.is_empty(), "expected a non-empty model catalog");
        let model = std::env::var("LIMEN_BENCH_MODEL").expect("set LIMEN_BENCH_MODEL");
        let out = client
            .complete(
                &model,
                &[ChatMessage::user("Reply with exactly: OK")],
                &CompletionParams {
                    temperature: 0.0,
                    max_tokens: 8,
                    seed: Some(1),
                },
            )
            .await
            .unwrap();
        assert!(!out.trim().is_empty());
    }
}
