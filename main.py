import torch
import asyncio
import flash_attn
import uvicorn
import gc
from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse

from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, AutoConfig, TextIteratorStreamer
from trl import AutoModelForCausalLMWithValueHead, PPOConfig, PPOTrainer
from peft import LoraConfig, get_peft_model


#model_path = "meta-llama/Meta-Llama-3-70B-Instruct"
model_path = "meta-llama/Meta-Llama-3-8B-Instruct"

app = FastAPI()

config = AutoConfig.from_pretrained(model_path)

model = AutoModelForCausalLM.from_pretrained(
    model_path,
    device_map='auto',
    config=config,
    torch_dtype=torch.bfloat16,
    attn_implementation="flash_attention_2"
)

tokenizer = AutoTokenizer.from_pretrained(model_path)
streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True) 

terminators = [
    tokenizer.eos_token_id,
    tokenizer.convert_tokens_to_ids("<|eot_id|>"),
]

async def generate_response(prompt: str):
    torch.cuda.empty_cache()
    gc.collect()
    inputs = tokenizer(prompt, return_tensors="pt").to("cuda")
    generation_kwargs = dict(
        inputs=inputs,
        streamer=streamer,
        do_sample=True,
        temperature=0.6,
        top_p=0.9,
    )

    task = asyncio.create_task(model.generate(**generation_kwargs))
    async for token in streamer:
        yield token
    await task

@app.get("/stream")
async def stream(prompt: str = Query(...)):
    return StreamingResponse(generate_response(prompt), media_type="text/plain")



if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000) 