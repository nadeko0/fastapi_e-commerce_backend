from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import Int32, RequestBody, ResponseBody, input_schema


class CategoryBase(BaseModel):
    """No length limit on the name: it is an input rule (CategoryCreate,
    CategoryUpdate), and a stored row that breaks it must still be readable."""
    name: str
    description: str | None = None
    parent_id: Int32 | None = None

class CategoryCreate(RequestBody, CategoryBase):
    """Schema for creating a new category"""
    name: str = Field(..., min_length=2, max_length=100)

    # validate_parent_id: an id is positive; null (a root category) is not an id.
    model_config = ConfigDict(json_schema_extra=input_schema(rules={"parent_id": {"exclusiveMinimum": 0}}))

    @field_validator('parent_id')
    @classmethod
    def validate_parent_id(cls, v):
        # parent_id can be None for root categories
        if v is not None and v <= 0:
            raise ValueError('parent_id must be a positive integer')
        return v

class CategoryInDB(CategoryBase):
    """Internal schema with all category fields"""
    id: int
    path: list[int] = Field(default_factory=list)  # Path to root for efficient traversal
    level: int  # Tree level (0 for root categories)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class CategoryResponse(ResponseBody, CategoryInDB):
    """Schema for API responses"""

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

