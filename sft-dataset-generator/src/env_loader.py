from dotenv import load_dotenv

load_dotenv()

import os

API_KEY = os.getenv("API_KEY")



# Use different models per stage to control cost:
#   - question generation: cheaper/faster model is fine (creativity, not precision)
#   - answer generation: your strongest model (grounding accuracy matters most)
#   - faithfulness check: cheaper model works well as a judge too

QUESTION_GEN_MODEL = os.getenv("QUESTION_GEN_MODEL")

ANSWER_GEN_MODEL = os.getenv("ANSWER_GEN_MODEL")

JUDGE_MODEL = os.getenv("JUDGE_MODEL")

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")

CHROMADB_COLLECTION_NAME=os.getenv("CHROMADB_COLLECTION_NAME")
CHROMADB_PATH = os.getenv("CHROMADB_PATH")

BUCKET_NAME = os.getenv("BUCKET_NAME")
