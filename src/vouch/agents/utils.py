from pathlib import Path

import frontmatter
from langchain_core.prompts import ChatPromptTemplate
from vouch.logger import get_logger

logger = get_logger(__file__)

def load_prompt(path: Path, **kwargs):
    try:
        document = frontmatter.load(path)

        prompt = ChatPromptTemplate.from_messages([
            ("system", document.content)
        ])

        return prompt.format(**kwargs)
    except Exception as e:
        logger.exception(f"Failed to load prompt: {path}\nError: {str(e)}")
        raise
