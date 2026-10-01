"""请求体模型与校验规则 (非法容量等在此明确报错)。"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, StrictInt


class PaperIn(BaseModel):
    paper_id: str = Field(min_length=1, max_length=128)
    manuscript: str = ""
    topics: List[str] = Field(default_factory=list)
    institutions: List[str] = Field(default_factory=list)


class PaperUpdate(BaseModel):
    manuscript: str = ""
    topics: List[str] = Field(default_factory=list)
    institutions: List[str] = Field(default_factory=list)


class ReviewerIn(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=128)
    credential: str = Field(min_length=1, max_length=256)
    topics: List[str] = Field(default_factory=list)
    institution: str = Field(min_length=1, max_length=256)
    capacity: StrictInt = Field(ge=1, le=100000, description="最大可评审论文数, 必须为 >= 1 的整数")
    avoid_papers: List[str] = Field(default_factory=list)


class ReviewerUpdate(BaseModel):
    credential: str = Field(min_length=1, max_length=256)
    topics: List[str] = Field(default_factory=list)
    institution: str = Field(min_length=1, max_length=256)
    capacity: StrictInt = Field(ge=1, le=100000)
    avoid_papers: List[str] = Field(default_factory=list)


class PublishIn(BaseModel):
    base_revision: int = Field(ge=0, description="预演结果中返回的资料修订号")


class AssignmentDecisionIn(BaseModel):
    paper_id: str = Field(min_length=1, max_length=128, description="当前发布版中分配给本人的论文编号")
    decision: Literal["confirm", "recuse"] = Field(description="confirm=确认接受, recuse=声明回避")
    reason: Optional[str] = Field(default=None, max_length=1000, description="回避原因; recuse 时必须非空")


class BackfillPublishIn(BaseModel):
    base_revision: int = Field(ge=0, description="补位预演结果中返回的资料修订号")
    base_serial: int = Field(ge=0, description="补位预演所基于的发布序号; 期间发生过任何发布则过期")
