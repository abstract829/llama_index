"""Dataset generation from documents"""
from __future__ import annotations

import re
from typing import List, Optional, Dict, Tuple
import json
import asyncio

from pydantic import BaseModel, Field

from llama_index import Document, ServiceContext, SummaryIndex
from llama_index.indices.postprocessor.node import KeywordNodePostprocessor
from llama_index.llms.openai import OpenAI
from llama_index.prompts.base import BasePromptTemplate, PromptTemplate
from llama_index.schema import BaseNode, MetadataMode, NodeWithScore
from llama_index.prompts.default_prompts import DEFAULT_TEXT_QA_PROMPT
import uuid

DEFAULT_QUESTION_GENERATION_PROMPT = """\
Context information is below.
---------------------
{context_str}
---------------------
Given the context information and not prior knowledge.
generate only questions based on the below query.
{query_str}
"""


def _get_default_service_context() -> ServiceContext:
    """Get default service context."""
    llm = OpenAI(temperature=0, model="gpt-3.5-turbo")
    service_context = ServiceContext.from_defaults(llm=llm, chunk_size_limit=3000)
    return service_context


class QueryResponseDataset(BaseModel):
    """Query Response Dataset.

    The response can be empty if the dataset is generated from documents.

    Args:
        queries (Dict[str, str]): Query id -> query.
        responses (Dict[str, str]): Query id -> response.

    """

    queries: Dict[str, str] = Field(
        default_factory=dict, description="Query id -> query"
    )
    responses: Dict[str, str] = Field(
        default_factory=dict, description="Query id -> response"
    )

    @property
    def qr_pairs(self) -> List[Tuple[str, str]]:
        """Get pairs."""
        # if query_id not in response, throw error
        for query_id in self.queries.keys():
            if query_id not in self.responses:
                raise ValueError(f"Query id {query_id} not in responses")

        return [
            (self.queries[query_id], self.responses[query_id])
            for query_id in self.queries.keys()
        ]

    @property
    def questions(self) -> List[str]:
        """Get questions."""
        return list(self.queries.values())

    def save_json(self, path: str) -> None:
        """Save json."""
        with open(path, "w") as f:
            json.dump(self.dict(), f, indent=4)

    @classmethod
    def from_json(cls, path: str) -> "QueryResponseDataset":
        """Load json."""
        with open(path, "r") as f:
            data = json.load(f)
        return cls(**data)


class DatasetGenerator:
    """Generate dataset (question/ question-answer pairs) \
    based on the given documents.

    NOTE: this is a beta feature, subject to change!

    Args:
        nodes (List[Node]): List of nodes. (Optional)
        service_context (ServiceContext): Service Context.
        num_questions_per_chunk: number of question to be \
        generated per chunk. Each document is chunked of size 512 words.
        text_question_template: Question generation template.
        question_gen_query: Question generation query.

    """

    def __init__(
        self,
        nodes: List[BaseNode],
        service_context: Optional[ServiceContext] = None,
        num_questions_per_chunk: int = 10,
        text_question_template: Optional[BasePromptTemplate] = None,
        text_qa_template: Optional[BasePromptTemplate] = None,
        question_gen_query: Optional[str] = None,
        metadata_mode: MetadataMode = MetadataMode.NONE,
        show_progress: bool = False,
    ) -> None:
        """Init params."""
        if service_context is None:
            service_context = _get_default_service_context()
        self.service_context = service_context
        self.text_question_template = text_question_template or PromptTemplate(
            DEFAULT_QUESTION_GENERATION_PROMPT
        )
        self.text_qa_template = text_qa_template or DEFAULT_TEXT_QA_PROMPT
        self.question_gen_query = (
            question_gen_query
            or f"You are a Teacher/Professor. Your task is to setup \
                        {num_questions_per_chunk} questions for an upcoming \
                        quiz/examination. The questions should be diverse in nature \
                            across the document. Restrict the questions to the \
                                context information provided."
        )
        self.nodes = nodes
        self._metadata_mode = metadata_mode
        self._show_progress = show_progress

    @classmethod
    def from_documents(
        cls,
        documents: List[Document],
        service_context: Optional[ServiceContext] = None,
        num_questions_per_chunk: int = 10,
        text_question_template: Optional[BasePromptTemplate] = None,
        text_qa_template: Optional[BasePromptTemplate] = None,
        question_gen_query: Optional[str] = None,
        required_keywords: Optional[List[str]] = None,
        exclude_keywords: Optional[List[str]] = None,
        show_progress: bool = False,
    ) -> "DatasetGenerator":
        """Generate dataset from documents."""
        if service_context is None:
            service_context = _get_default_service_context()
        nodes = service_context.node_parser.get_nodes_from_documents(documents)

        # use node postprocessor to filter nodes
        required_keywords = required_keywords or []
        exclude_keywords = exclude_keywords or []
        node_postprocessor = KeywordNodePostprocessor(
            service_context=service_context,
            required_keywords=required_keywords,
            exclude_keywords=exclude_keywords,
        )
        node_with_scores = [NodeWithScore(node=node) for node in nodes]
        node_with_scores = node_postprocessor.postprocess_nodes(node_with_scores)
        nodes = [node_with_score.node for node_with_score in node_with_scores]

        return cls(
            nodes=nodes,
            service_context=service_context,
            num_questions_per_chunk=num_questions_per_chunk,
            text_question_template=text_question_template,
            text_qa_template=text_qa_template,
            question_gen_query=question_gen_query,
            show_progress=show_progress,
        )

    async def _agenerate_dataset(
        self,
        nodes: List[BaseNode],
        num: Optional[int] = None,
        generate_response: bool = False,
    ) -> QueryResponseDataset:
        """Node question generator."""

        query_tasks = []
        queries: Dict[str, str] = {}
        responses_dict: Dict[str, str] = {}

        if self._show_progress:
            from tqdm.asyncio import tqdm_asyncio

            async_module = tqdm_asyncio
        else:
            async_module = asyncio

        summary_indices: List[SummaryIndex] = []
        for node in nodes:
            if num is not None and len(queries) >= num:
                break
            index = SummaryIndex.from_documents(
                [
                    Document(
                        text=node.get_content(metadata_mode=self._metadata_mode),
                        metadata=node.metadata,
                    )
                ],
                service_context=self.service_context,
            )

            query_engine = index.as_query_engine(
                service_context=self.service_context,
                text_qa_template=self.text_question_template,
                use_async=True,
            )
            task = query_engine.aquery(
                self.question_gen_query,
            )
            query_tasks.append(task)
            summary_indices.append(index)

        responses = await async_module.gather(*query_tasks)
        for idx, response in enumerate(responses):
            result = str(response).strip().split("\n")
            cleaned_questions = [
                re.sub(r"^\d+[\).\s]", "", question).strip() for question in result
            ]
            cleaned_questions = [
                question for question in cleaned_questions if len(question) > 0
            ]
            cur_queries = {
                str(uuid.uuid4()): question for question in cleaned_questions
            }
            queries.update(cur_queries)

            if generate_response:
                index = summary_indices[idx]
                qr_tasks = []
                cur_query_items = list(cur_queries.items())
                cur_query_keys = [query_id for query_id, _ in cur_query_items]
                for query_id, query in cur_query_items:
                    qa_query_engine = index.as_query_engine(
                        service_context=self.service_context,
                        text_qa_template=self.text_qa_template,
                    )
                    qr_task = qa_query_engine.aquery(query)
                    qr_tasks.append(qr_task)
                qr_responses = await async_module.gather(*qr_tasks)
                for query_id, qa_response in zip(cur_query_keys, qr_responses):
                    responses_dict[query_id] = str(qa_response)
            else:
                pass

        query_ids = list(queries.keys())
        if num is not None:
            query_ids = query_ids[:num]
            # truncate queries, responses to the subset of query ids
            queries = {query_id: queries[query_id] for query_id in query_ids}
            if generate_response:
                responses_dict = {
                    query_id: responses_dict[query_id] for query_id in query_ids
                }

        return QueryResponseDataset(queries=queries, responses=responses_dict)

    async def agenerate_questions_from_nodes(
        self, num: Optional[int] = None
    ) -> List[str]:
        """Generates questions for each document."""
        dataset = await self._agenerate_dataset(
            self.nodes, num=num, generate_response=False
        )
        return dataset.questions

    async def agenerate_dataset_from_nodes(
        self, num: Optional[int] = None
    ) -> QueryResponseDataset:
        """Generates questions for each document."""
        dataset = await self._agenerate_dataset(
            self.nodes, num=num, generate_response=True
        )
        return dataset

    def generate_questions_from_nodes(self, num: Optional[int] = None) -> List[str]:
        """Generates questions for each document."""
        return asyncio.run(self.agenerate_questions_from_nodes(num=num))

    def generate_dataset_from_nodes(
        self, num: Optional[int] = None
    ) -> QueryResponseDataset:
        """Generates questions for each document."""
        return asyncio.run(self.agenerate_dataset_from_nodes(num=num))
