import base64
import io
import logging
import re
from typing import Any, Dict, List, Optional, Union

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer

app = FastAPI(title="InternVL2.5-4B-MPO OpenAI API Server")
MODEL_DIR = r"C:\AI\models\InternVL2_5-4B-MPO"
MODEL_NAME = "InternVL2.5-4B-MPO"
MAX_IMAGES = 12

print(f"Loading {MODEL_NAME} from {MODEL_DIR}...", flush=True)
tokenizer = AutoTokenizer.from_pretrained(
    MODEL_DIR, trust_remote_code=True, use_fast=False
)
model = AutoModel.from_pretrained(
    MODEL_DIR,
    torch_dtype=torch.float16,
    device_map="cuda:0",
    trust_remote_code=True,
    low_cpu_mem_usage=True,
).eval()
print(f"{MODEL_NAME} loaded successfully on GPU!", flush=True)


class ContentItem(BaseModel):
    type: str
    text: Optional[str] = None
    image_url: Optional[Dict[str, str]] = None
    url: Optional[str] = None


class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[ContentItem]]


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = MODEL_NAME
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.0
    max_tokens: Optional[int] = 1024


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "checkpoint": MODEL_DIR,
        "cuda": torch.cuda.is_available(),
    }


def parse_image(url_str: str) -> Image.Image:
    if url_str.startswith("data:image"):
        img_bytes = base64.b64decode(url_str.split(",", 1)[1])
    else:
        import requests

        response = requests.get(url_str, timeout=15)
        response.raise_for_status()
        img_bytes = response.content
    return Image.open(io.BytesIO(img_bytes)).convert("RGB")


def content_parts(message: ChatMessage) -> tuple[str, list[str]]:
    if isinstance(message.content, str):
        return message.content.strip(), []
    text_parts = []
    image_urls = []
    for item in message.content:
        if item.type in ("text", "input_text") and item.text:
            text_parts.append(item.text)
        elif item.type in ("image_url", "image", "input_image"):
            url = item.url or ((item.image_url or {}).get("url", ""))
            if url:
                image_urls.append(url)
    return "\n".join(text_parts).strip(), image_urls


def extract_request(messages: List[ChatMessage]):
    system_parts = []
    turns = []
    current_index = None
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            current_index = index
            break
    if current_index is None:
        raise ValueError("messages must contain a user message")

    pending_user = None
    for index, message in enumerate(messages):
        text, image_urls = content_parts(message)
        if message.role == "system":
            if text:
                system_parts.append(text)
        elif index < current_index and message.role == "user":
            if pending_user is not None:
                turns.append((pending_user, ""))
            pending_user = text
        elif index < current_index and message.role == "assistant":
            if pending_user is not None:
                turns.append((pending_user, text))
                pending_user = None

    if pending_user is not None:
        turns.append((pending_user, ""))

    current_text, current_images = content_parts(messages[current_index])
    image_urls = list(current_images)
    image_urls = image_urls[:MAX_IMAGES]
    markers = "\n".join(f"Image-{index}: <image>" for index in range(1, len(image_urls) + 1))
    query_parts = ["\n\n".join(system_parts), current_text, markers]
    query = "\n\n".join(part for part in query_parts if part).strip() or "Hello"
    return query, turns, image_urls


def prepare_images(image_urls: list[str]):
    if not image_urls:
        return None, None
    from torchvision import transforms

    transform = transforms.Compose([
        transforms.Resize((448, 448)),
        transforms.ToTensor(),
        transforms.Normalize(
            mean=[0.485, 0.456, 0.406],
            std=[0.229, 0.224, 0.225],
        ),
    ])
    pixel_values = torch.cat([
        transform(parse_image(url)).unsqueeze(0) for url in image_urls
    ], dim=0).to(dtype=torch.float16, device="cuda:0")
    return pixel_values, [1] * len(image_urls)


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    try:
        query, history, image_urls = extract_request(req.messages)
        pixel_values, num_patches_list = prepare_images(image_urls)
        temperature = float(req.temperature or 0.0)
        generation_config = {
            "max_new_tokens": max(1, min(int(req.max_tokens or 1024), 4096)),
            "do_sample": temperature > 0,
            "repetition_penalty": 1.05,
        }
        if temperature > 0:
            generation_config["temperature"] = min(temperature, 2.0)

        with torch.inference_mode():
            response = model.chat(
                tokenizer,
                pixel_values,
                query,
                generation_config,
                num_patches_list=num_patches_list,
                history=history or None,
                return_history=False,
            )
        response = re.sub(r"<think>.*?(?:</think>|$)", "", str(response or ""), flags=re.DOTALL).strip()
        return {
            "id": "chatcmpl-internvl2.5-mpo",
            "object": "chat.completion",
            "model": req.model or MODEL_NAME,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": response},
                "finish_reason": "stop",
            }],
        }
    except Exception as exc:
        logging.exception("InternVL request failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=23333)
