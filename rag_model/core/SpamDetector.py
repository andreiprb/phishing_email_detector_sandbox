import os
import re
from dotenv import load_dotenv
from typing import Tuple, Optional, List, Dict, Any

from llama_index.core import load_index_from_storage, StorageContext, Settings
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.base.llms.base import BaseLLM
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.llms.groq import Groq
from llama_index.core.schema import NodeWithScore

load_dotenv()


class SpamDetector:
    """A robust spam detector using LlamaIndex vector stores with careful prompt engineering."""

    def __init__(
            self,
            llm: Optional[BaseLLM] = None,
            embed_model: Optional[BaseEmbedding] = None,
            spam_index_path: str = "../data/index_spam",
            ham_index_path: str = "../data/index_ham",
            top_k: int = 2
    ):
        """
        Initialize the SpamDetector with LLM and vector stores.

        Args:
            spam_index_path: Path to the spam vector store
            ham_index_path: Path to the ham vector store
            llm: LLM instance (default: Groq with temperature 0)
            top_k: Number of examples to retrieve from each index
        """
        if llm is None:
            api_key = os.getenv("GROQ_API_KEY")
            assert api_key is not None, "GROQ_API_KEY environment variable not set."
            self.llm = Groq(model="gemma2-9b-it", temperature=0, api_key=api_key)
        else:
            self.llm = llm

        if embed_model is None:
            self.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-en-v1.5")
        else:
            self.embed_model = embed_model

        Settings.llm = self.llm
        Settings.embed_model = self.embed_model

        self.top_k = top_k
        self.spam_index = self._load_index(spam_index_path)
        self.ham_index = self._load_index(ham_index_path)

    def _load_index(self, index_path: str):
        """Load an index from storage."""
        if os.path.exists(index_path):
            storage_context = StorageContext.from_defaults(persist_dir=index_path)
            return load_index_from_storage(storage_context)
        else:
            raise FileNotFoundError(f"Index not found at {index_path}")

    def classify(self, email_text: str, metadata: Optional[Dict[str, Any]] = None) -> Tuple[str, float]:
        """
        Classify an email as spam or ham using vector retrieval + LLM.

        Args:
            email_text: The text content of the email to classify
            metadata: Optional dictionary containing email metadata like sender, subject,
                     attachment info, headers, etc.

        Returns:
            Tuple containing classification ('spam' or 'ham') and confidence score
        """
        # Query both indices to retrieve similar examples
        spam_query_engine = self.spam_index.as_query_engine(similarity_top_k=self.top_k)
        ham_query_engine = self.ham_index.as_query_engine(similarity_top_k=self.top_k)

        spam_response = spam_query_engine.query(email_text)
        ham_response = ham_query_engine.query(email_text)

        spam_examples = spam_response.source_nodes
        ham_examples = ham_response.source_nodes

        prompt = self._create_classification_prompt(email_text, spam_examples, ham_examples, metadata)

        response = self.llm.complete(prompt)

        classification, confidence = self._parse_classification_response(response.text)

        return classification, confidence

    def _create_classification_prompt(self, email_text: str, spam_examples: List[NodeWithScore],
                                      ham_examples: List[NodeWithScore],
                                      metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Create a prompt for the LLM with examples, metadata, and the email to classify.

        Args:
            email_text: The email text to classify
            spam_examples: List of spam examples with similarity scores
            ham_examples: List of ham examples with similarity scores
            metadata: Optional dictionary containing email metadata

        Returns:
            String prompt for the LLM
        """
        prompt = "You are an expert spam email detector. Analyze the email below and classify it as 'spam' or 'ham' (not spam).\n\n"

        prompt += "## SIMILAR SPAM EXAMPLES:\n"
        for i, example in enumerate(spam_examples):
            prompt += f"Spam Example {i + 1} (similarity: {example.score:.4f}):\n"
            prompt += f"{example.node.get_content()}\n"

            if hasattr(example.node, 'metadata') and example.node.metadata:
                prompt += "Example Metadata:\n"
                for key, value in example.node.metadata.items():
                    prompt += f"- {key}: {value}\n"
            prompt += "\n"

        prompt += "## SIMILAR HAM EXAMPLES:\n"
        for i, example in enumerate(ham_examples):
            prompt += f"Ham Example {i + 1} (similarity: {example.score:.4f}):\n"
            prompt += f"{example.node.get_content()}\n"

            if hasattr(example.node, 'metadata') and example.node.metadata:
                prompt += "Example Metadata:\n"
                for key, value in example.node.metadata.items():
                    prompt += f"- {key}: {value}\n"
            prompt += "\n"

        prompt += "## EMAIL TO CLASSIFY:\n"
        prompt += email_text + "\n\n"

        if metadata:
            prompt += "## EMAIL METADATA:\n"
            for key, value in metadata.items():
                prompt += f"- {key}: {value}\n"
            prompt += "\n"

        prompt += """
            Based on both the email content and metadata, classify the email as 'spam' or 'ham'.

            Provide your reasoning first, analyzing:
            1. The email's content and language patterns
            2. The provided metadata (sender domain, headers, etc.)
            3. Similarity to the examples
            4. Any suspicious patterns in attachments or links

            Then provide your final classification in this exact format:
            CLASSIFICATION: [spam/ham]
            CONFIDENCE: [0.0-1.0]

            The confidence score should be between 0.0 and 1.0, where 1.0 indicates complete certainty.
            """

        # print(prompt)
        return prompt

    def _parse_classification_response(self, response_text: str) -> Tuple[str, float]:
        """
        Parse the LLM's response to extract classification and confidence.

        Args:
            response_text: The text response from the LLM

        Returns:
            Tuple of (classification, confidence)
        """
        try:
            classification = None
            if "CLASSIFICATION: spam" in response_text.lower():
                classification = "spam"
            elif "CLASSIFICATION: ham" in response_text.lower():
                classification = "ham"

            confidence = 0.5
            confidence_match = re.search(r"CONFIDENCE:\s*(0\.\d+|1\.0)", response_text)
            if confidence_match:
                confidence = float(confidence_match.group(1))

            if classification is None:
                if "spam" in response_text.lower() and "not spam" not in response_text.lower():
                    classification = "spam"
                else:
                    classification = "ham"

            return classification, confidence

        except Exception as e:
            print(f"Error parsing LLM response: {e}")
            return "ham", 0.5
