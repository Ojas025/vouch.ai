from pathlib import Path

import frontmatter
from langchain_core.prompts import ChatPromptTemplate
from vouch.logger import get_logger

logger = get_logger(__file__)

def load_prompt(path: Path, **kwargs):
    try:
        document = frontmatter.load(path)

        # Role prompts carry per-run placeholders ({investigation_id}, {task}, ...)
        # that are injected via messages at invoke time; only format when the
        # caller actually supplies values.
        if not kwargs:
            return document.content

        prompt = ChatPromptTemplate.from_messages([
            ("system", document.content)
        ])

        return prompt.format(**kwargs)
    except Exception as e:
        logger.exception(f"Failed to load prompt: {path}\nError: {str(e)}")
        raise
