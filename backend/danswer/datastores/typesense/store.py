import json
from functools import partial
from typing import Any
from uuid import UUID

import typesense  # type: ignore
from danswer.chunking.models import EmbeddedIndexChunk
from danswer.chunking.models import IndexChunk
from danswer.chunking.models import InferenceChunk
from danswer.configs.app_configs import TYPESENSE_DEFAULT_COLLECTION
from danswer.configs.constants import ALLOWED_GROUPS
from danswer.configs.constants import ALLOWED_USERS
from danswer.configs.constants import BLURB
from danswer.configs.constants import CHUNK_ID
from danswer.configs.constants import CONTENT
from danswer.configs.constants import DOCUMENT_ID
from danswer.configs.constants import PUBLIC_DOC_PAT
from danswer.configs.constants import SECTION_CONTINUATION
from danswer.configs.constants import SEMANTIC_IDENTIFIER
from danswer.configs.constants import SOURCE_LINKS
from danswer.configs.constants import SOURCE_TYPE
from danswer.datastores.datastore_utils import DEFAULT_BATCH_SIZE
from danswer.datastores.datastore_utils import get_uuid_from_chunk
from danswer.datastores.datastore_utils import update_doc_user_map
from danswer.datastores.interfaces import IndexFilter
from danswer.datastores.interfaces import KeywordIndex
from danswer.utils.clients import get_typesense_client
from danswer.utils.logging import setup_logger
from typesense.exceptions import ObjectNotFound  # type: ignore


logger = setup_logger()


def check_typesense_collection_exist(
    collection_name: str = TYPESENSE_DEFAULT_COLLECTION,
) -> bool:
    client = get_typesense_client()
    try:
        client.collections[collection_name].retrieve()
    except ObjectNotFound:
        return False
    return True


def create_typesense_collection(
    collection_name: str = TYPESENSE_DEFAULT_COLLECTION,
) -> None:
    ts_client = get_typesense_client()
    collection_schema = {
        "name": collection_name,
        "fields": [
            # Typesense uses "id" type string as a special field
            {"name": "id", "type": "string"},
            {"name": DOCUMENT_ID, "type": "string"},
            {"name": CHUNK_ID, "type": "int32"},
            {"name": BLURB, "type": "string"},
            {"name": CONTENT, "type": "string"},
            {"name": SOURCE_TYPE, "type": "string"},
            {"name": SOURCE_LINKS, "type": "string"},
            {"name": SEMANTIC_IDENTIFIER, "type": "string"},
            {"name": SECTION_CONTINUATION, "type": "bool"},
            {"name": ALLOWED_USERS, "type": "string[]"},
            {"name": ALLOWED_GROUPS, "type": "string[]"},
        ],
    }
    ts_client.collections.create(collection_schema)


def get_typesense_document_whitelists(
    doc_chunk_id: str, collection_name: str, ts_client: typesense.Client
) -> tuple[bool, list[str], list[str]]:
    """Returns whether the document already exists and the users/group whitelists"""
    try:
        document = (
            ts_client.collections[collection_name].documents[doc_chunk_id].retrieve()
        )
    except ObjectNotFound:
        return False, [], []
    if document[ALLOWED_USERS] is None or document[ALLOWED_GROUPS] is None:
        raise RuntimeError(
            "Typesense Index is corrupted, Document found with no access lists."
        )
    return True, document[ALLOWED_USERS], document[ALLOWED_GROUPS]


def delete_typesense_doc_chunks(
    document_id: str, collection_name: str, ts_client: typesense.Client
) -> bool:
    doc_id_filter = {"filter_by": f"{DOCUMENT_ID}:'{document_id}'"}

    # Typesense doesn't seem to prioritize individual deletions, problem not seen with this approach
    # Point to consider if we see instances of number of Typesense and Qdrant docs not matching
    del_result = ts_client.collections[collection_name].documents.delete(doc_id_filter)
    return del_result["num_deleted"] != 0


def index_typesense_chunks(
    chunks: list[IndexChunk | EmbeddedIndexChunk],
    user_id: UUID | None,
    collection: str,
    client: typesense.Client | None = None,
    batch_upsert: bool = True,
) -> int:
    user_str = PUBLIC_DOC_PAT if user_id is None else str(user_id)
    ts_client: typesense.Client = client if client else get_typesense_client()

    new_documents: list[dict[str, Any]] = []
    doc_user_map: dict[str, dict[str, list[str]]] = {}
    docs_deleted = 0
    for chunk in chunks:
        document = chunk.source_document
        doc_user_map, delete_doc = update_doc_user_map(
            chunk,
            doc_user_map,
            partial(
                get_typesense_document_whitelists,
                collection_name=collection,
                ts_client=ts_client,
            ),
            user_str,
        )

        if delete_doc:
            # Processing the first chunk of the doc and the doc exists
            docs_deleted += 1
            delete_typesense_doc_chunks(document.id, collection, ts_client)

        new_documents.append(
            {
                "id": str(get_uuid_from_chunk(chunk)),  # No minichunks for typesense
                DOCUMENT_ID: document.id,
                CHUNK_ID: chunk.chunk_id,
                BLURB: chunk.blurb,
                CONTENT: chunk.content,
                SOURCE_TYPE: str(document.source.value),
                SOURCE_LINKS: json.dumps(chunk.source_links),
                SEMANTIC_IDENTIFIER: document.semantic_identifier,
                SECTION_CONTINUATION: chunk.section_continuation,
                ALLOWED_USERS: doc_user_map[document.id][ALLOWED_USERS],
                ALLOWED_GROUPS: doc_user_map[document.id][ALLOWED_GROUPS],
            }
        )

    if batch_upsert:
        doc_batches = [
            new_documents[x : x + DEFAULT_BATCH_SIZE]
            for x in range(0, len(new_documents), DEFAULT_BATCH_SIZE)
        ]
        for doc_batch in doc_batches:
            results = ts_client.collections[collection].documents.import_(
                doc_batch, {"action": "upsert"}
            )
            failures = [
                doc_res["success"]
                for doc_res in results
                if doc_res["success"] is not True
            ]
            logger.info(
                f"Indexed {len(doc_batch)} chunks into Typesense collection '{collection}', "
                f"number failed: {len(failures)}"
            )
    else:
        [
            ts_client.collections[collection].documents.upsert(document)
            for document in new_documents
        ]

    return len(doc_user_map.keys()) - docs_deleted


def _build_typesense_filters(
    user_id: UUID | None, filters: list[IndexFilter] | None
) -> str:
    filter_str = ""

    # Permissions filter
    if user_id:
        filter_str += f"{ALLOWED_USERS}:=[{PUBLIC_DOC_PAT},{user_id}] && "
    else:
        filter_str += f"{ALLOWED_USERS}:={PUBLIC_DOC_PAT} && "

    # Provided query filters
    if filters:
        for filter_dict in filters:
            valid_filters = {
                key: value for key, value in filter_dict.items() if value is not None
            }
            for filter_key, filter_val in valid_filters.items():
                if isinstance(filter_val, str):
                    filter_str += f"{filter_key}:={filter_val} && "
                elif isinstance(filter_val, list):
                    filters_or = ",".join([str(f_val) for f_val in filter_val])
                    filter_str += f"{filter_key}:=[{filters_or}] && "
                else:
                    raise ValueError("Invalid filters provided")
    if filter_str[-4:] == " && ":
        filter_str = filter_str[:-4]
    return filter_str


class TypesenseIndex(KeywordIndex):
    def __init__(self, collection: str = TYPESENSE_DEFAULT_COLLECTION) -> None:
        self.collection = collection
        self.ts_client = get_typesense_client()

    def index(self, chunks: list[IndexChunk], user_id: UUID | None) -> int:
        return index_typesense_chunks(
            chunks=chunks,
            user_id=user_id,
            collection=self.collection,
            client=self.ts_client,
        )

    def keyword_search(
        self,
        query: str,
        user_id: UUID | None,
        filters: list[IndexFilter] | None,
        num_to_retrieve: int,
    ) -> list[InferenceChunk]:
        filters_str = _build_typesense_filters(user_id, filters)

        search_query = {
            "q": query,
            "query_by": CONTENT,
            "filter_by": filters_str,
            "per_page": num_to_retrieve,
            "limit_hits": num_to_retrieve,
            "num_typos": 2,
        }

        search_results = self.ts_client.collections[self.collection].documents.search(
            search_query
        )

        hits = search_results["hits"]
        inference_chunks = [InferenceChunk.from_dict(hit["document"]) for hit in hits]

        return inference_chunks
