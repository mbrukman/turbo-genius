import torch
import flash_attn
import uvicorn
import gc
import asyncio
import argparse
from fastapi import FastAPI, WebSocket
from threading import Thread

from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, AutoConfig, TextIteratorStreamer, pipeline
from trl import AutoModelForCausalLMWithValueHead, PPOConfig, PPOTrainer
from peft import LoraConfig, get_peft_model

from session import Session, SessionManager


app = FastAPI()

parser = argparse.ArgumentParser()
parser.add_argument('--model', action='store', default="meta-llama/Meta-Llama-3-8B-Instruct")
args = parser.parse_args()

bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16
)

config = AutoConfig.from_pretrained(args.model)

model = AutoModelForCausalLM.from_pretrained(
    args.model,
    device_map='auto',
    config=config,
    quantization_config=bnb_config,
    attn_implementation="flash_attention_2"
)

tokenizer = AutoTokenizer.from_pretrained(args.model)

terminators = [
    tokenizer.eos_token_id,
    tokenizer.convert_tokens_to_ids("<|eot_id|>"),
]

summarizer = pipeline(task="summarization", model="facebook/bart-large", min_length=2, max_length=8)


async def stream_tokens(streamer: TextIteratorStreamer):
    for token in streamer:
        yield token
    yield None

async def generate_response(prompt: str):
    torch.cuda.empty_cache()
    gc.collect()
    inputs = tokenizer(prompt, return_tensors="pt").to("cuda")
    streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True) 
    generation_kwargs = {
        "input_ids": inputs["input_ids"],
        "attention_mask": inputs["attention_mask"],
        "streamer": streamer,
        "do_sample": True,
        "temperature": 0.6,
        "top_p": 0.9,
        "max_length": config.max_position_embeddings,
    }

    # Run the generation in a separate thread
    thread = Thread(target=model.generate, kwargs=generation_kwargs)
    thread.start()

    # Start streaming tokens
    async for token in stream_tokens(streamer):
        yield token

    thread.join()

async def make_title(session: Session):
    messages = session.get_messages()[1:3]
    prompt = "\n".join([message["content"] for message in messages])
    return summarizer(prompt)

def make_prompt(session: Session):
    inputs = tokenizer.apply_chat_template(
        session.get_messages(),
        add_generation_prompt=True,
        return_tensors="pt",
        tokenize=True
    )
    num_tokens = inputs.shape[-1]
    print(f"Number of tokens (session {session.session_id}): ", num_tokens)
    if num_tokens > int(config.max_position_embeddings * 0.9):
        session.truncate_messages()
        return make_prompt(session)
    else:
        return tokenizer.apply_chat_template(
            session.get_messages(),
            add_generation_prompt=True,
            tokenize=False
        )

@app.websocket("/stream/{session_id}")
async def stream(websocket: WebSocket, session_id: int):
    await websocket.accept()
    message = await websocket.receive_text()
    session = session_manager.get_session(session_id)
    session.add_user_message(message)
    prompt = make_prompt(session)
    completion = ""
    try:
        async for token in generate_response(prompt):
            if token is None:
                break
            completion += token
            await websocket.send_text(token)
            await asyncio.sleep(0.01)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        session.add_assistant_message(completion)
        await websocket.close()

@app.get("/session")
async def get_session():
    session = session_manager.get_new_session()
    return session.session_id

@app.get("/session/{session_id}")
async def get_session(session_id: int):
    session = session_manager.get_session(session_id)
    return session

@app.get("/session-list")
async def get_session_list():
    sessions = session_manager.get_session_list()
    return sessions

@app.delete("/session/{session_id}")
async def delete_session(session_id: int):
    session_manager.remove_session(session_id)
    return

@app.get("/session/{session_id}/title")
async def get_session_title(session_id: int):
    session = session_manager.get_session(session_id)
    summary_response = await make_title(session)
    session.title = summary_response[0]["summary_text"]
    return session.title

if __name__ == "__main__":
    session_manager = SessionManager()
    uvicorn.run(app, host="0.0.0.0", port=8000)