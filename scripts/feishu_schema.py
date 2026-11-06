"""The Feishu-side shape of a sounding library.

Notion models the library as one database of papers plus four side databases it
links to. Bitable has the same building blocks, so the mapping is close to 1:1
and the note template does not need a second vocabulary:

    papers  论文 Papers   one row per citekey, scalar fields + multi selects
    topics  课题           linked from papers.课题
    fields  领域           linked from papers.领域
    waves   潮             linked from papers.潮
    labs    课题组         linked from papers.课题组

Bitable field types used here:

    1  text            2  number          3  single select
    4  multi select    15 url             18 single link (holds many records)
    17 attachment

Field names are Chinese on purpose: they are the same strings the Notion database
uses, so ``reference-library.md`` §2 maps one vocabulary, not two.
"""
from __future__ import annotations

TEXT, NUMBER, SINGLE, MULTI, URL, LINK = 1, 2, 3, 4, 15, 18

# Concept -> (bitable field name, type). Kept explicit so a mismatch is a
# one-line change instead of a hunt through the publisher.
PAPER_FIELDS = [
    ("标题", TEXT),
    ("citekey", TEXT),
    ("发表", TEXT),
    ("DOI", TEXT),
    ("年份", NUMBER),
    ("类型", SINGLE),
    ("读法", SINGLE),
    ("角色", SINGLE),
    ("数据类型", MULTI),
    ("开创了什么", TEXT),
    ("世代名", TEXT),
    ("笔记", URL),
]

# Links are added after every table exists, because a link needs the target id.
PAPER_LINKS = [
    ("课题", "topics"),
    ("课题组", "labs"),
    ("领域", "fields"),
    ("潮", "waves"),
    ("世代", "papers"),
    ("借自", "papers"),
    ("追随者", "papers"),
]

OPTIONS = {
    "类型": ["algorithm", "science", "review", "other"],
    "读法": ["扫", "精读"],
    "角色": ["创世", "追随", "旁支", "方法件", "未定"],
}

TABLES = {
    "papers": {"name": "论文 Papers", "primary": "标题",
               "fields": PAPER_FIELDS, "links": PAPER_LINKS},
    "topics": {"name": "课题", "primary": "名称", "fields": [("名称", TEXT), ("一句话", TEXT)]},
    "fields": {"name": "领域", "primary": "名称", "fields": [("名称", TEXT), ("一句话", TEXT)]},
    "waves": {"name": "潮", "primary": "名称", "fields": [("名称", TEXT), ("一句话", TEXT)]},
    "labs": {"name": "课题组", "primary": "名称", "fields": [("名称", TEXT), ("机构", TEXT)]},
}

# The concept keys the skill uses in a note's property block. Long form maps the
# note key to the bitable field name; several note keys share one links table.
CONCEPT_TO_FIELD = {
    "title": "标题",
    "citekey": "citekey",
    "venue": "发表",
    "year": "年份",
    "type": "类型",
    "reading": "读法",
    "doi": "DOI",
    "role": "角色",
    "data": "数据类型",
    "opened": "开创了什么",
    "generation_name": "世代名",
    "note_url": "笔记",
    "topic": "课题",
    "group": "课题组",
    "domain": "领域",
    "wave": "潮",
    "generation": "世代",
    "borrowed_from": "借自",
    "followers": "追随者",
}

SELECT_FIELDS = {"类型", "读法", "角色"}
MULTI_FIELDS = {"数据类型"}
LINK_FIELDS = {name for name, _ in PAPER_LINKS}
NUMBER_FIELDS = {"年份"}
URL_FIELDS = {"笔记"}
TEXT_FIELDS = {"标题", "citekey", "发表", "DOI", "开创了什么", "世代名"}


def field_spec(name, ftype):
    """Bitable's create-field payload for one field."""
    spec = {"field_name": name, "type": ftype}
    if name in OPTIONS:
        spec["property"] = {"options": [{"name": o} for o in OPTIONS[name]]}
    return spec


def link_spec(name, target_table_id):
    return {"field_name": name, "type": LINK,
            "property": {"table_id": target_table_id}}


def table_of_link(name):
    for link_name, table_key in PAPER_LINKS:
        if link_name == name:
            return table_key
    return None
