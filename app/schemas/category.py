from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import Int32, RequestBody


class CategoryBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    description: str | None = None
    parent_id: Int32 | None = None

class CategoryCreate(RequestBody, CategoryBase):
    """Schema for creating a new category"""
    metadata: dict | None = Field(default_factory=dict)

    @field_validator('parent_id')
    @classmethod
    def validate_parent_id(cls, v):
        # parent_id can be None for root categories
        if v is not None and v <= 0:
            raise ValueError('parent_id must be a positive integer')
        return v

class CategoryUpdate(RequestBody):
    """Schema for updating an existing category"""
    name: str | None = Field(None, min_length=2, max_length=100)
    description: str | None = None
    parent_id: Int32 | None = None
    metadata: dict | None = None

    @field_validator('parent_id')
    @classmethod
    def validate_parent_id(cls, v):
        if v is not None and v <= 0:
            raise ValueError('parent_id must be a positive integer')
        return v

class CategoryInDB(CategoryBase):
    """Internal schema with all category fields"""
    id: int
    path: list[int] = Field(default_factory=list)  # Path to root for efficient traversal
    level: int = Field(ge=0)  # Tree level (0 for root categories)
    created_at: datetime
    updated_at: datetime
    metadata: dict = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)

    @field_validator('metadata', mode="before")
    @classmethod
    def coerce_missing_metadata(cls, v):
        # app.models.category.Category has no `metadata` column. Every
        # SQLAlchemy declarative model exposes its own class-level
        # `metadata` (the mapper's MetaData registry, not a dict) via
        # `Base.metadata`, and with from_attributes=True that shadows this
        # field on every ORM-loaded instance - so every GET that returns a
        # category (list/detail/tree) raised a 500 ValidationError before
        # this coercion. There is no real persisted category metadata
        # today; treat any non-dict source as empty rather than reject it.
        return v if isinstance(v, dict) else {}

class CategoryResponse(CategoryInDB):
    """Schema for API responses"""
    product_count: int = 0
    children_count: int = 0

    model_config = ConfigDict(from_attributes=True)

class CategoryTreeNode(CategoryResponse):
    """Schema for tree-structured responses"""
    children: list["CategoryTreeNode"] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)

# Self-referential types need to be declared after the class
CategoryTreeNode.model_rebuild()

class CategoryListResponse(BaseModel):
    """Schema for paginated category list"""
    items: list[CategoryResponse]
    total: int
    page: int
    size: int
    has_more: bool

    model_config = ConfigDict(from_attributes=True)

class CategoryTreeResponse(BaseModel):
    """Schema for complete category tree"""
    tree: list[CategoryTreeNode]
    total_categories: int
    max_depth: int

    model_config = ConfigDict(from_attributes=True)

