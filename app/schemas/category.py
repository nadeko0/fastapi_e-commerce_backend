from datetime import datetime
from typing import List, Optional, Dict
from pydantic import BaseModel, Field, ConfigDict, validator

class CategoryBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = None
    parent_id: Optional[int] = None

class CategoryCreate(CategoryBase):
    """Schema for creating a new category"""
    metadata: Optional[Dict] = Field(default_factory=dict)

    @validator('parent_id')
    def validate_parent_id(cls, v):
        # parent_id can be None for root categories
        if v is not None and v <= 0:
            raise ValueError('parent_id must be a positive integer')
        return v

class CategoryUpdate(BaseModel):
    """Schema for updating an existing category"""
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    description: Optional[str] = None
    parent_id: Optional[int] = None
    metadata: Optional[Dict] = None

    @validator('parent_id')
    def validate_parent_id(cls, v):
        if v is not None and v <= 0:
            raise ValueError('parent_id must be a positive integer')
        return v

class CategoryInDB(CategoryBase):
    """Internal schema with all category fields"""
    id: int
    path: List[int] = Field(default_factory=list)  # Path to root for efficient traversal
    level: int = Field(ge=0)  # Tree level (0 for root categories)
    created_at: datetime
    updated_at: datetime
    metadata: Dict = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)

class CategoryResponse(CategoryInDB):
    """Schema for API responses"""
    product_count: int = 0
    children_count: int = 0

    model_config = ConfigDict(from_attributes=True)

class CategoryTreeNode(CategoryResponse):
    """Schema for tree-structured responses"""
    children: List["CategoryTreeNode"] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)

# Self-referential types need to be declared after the class
CategoryTreeNode.model_rebuild()

class CategoryListResponse(BaseModel):
    """Schema for paginated category list"""
    items: List[CategoryResponse]
    total: int
    page: int
    size: int
    has_more: bool

    model_config = ConfigDict(from_attributes=True)

class CategoryTreeResponse(BaseModel):
    """Schema for complete category tree"""
    tree: List[CategoryTreeNode]
    total_categories: int
    max_depth: int

    model_config = ConfigDict(from_attributes=True)

class CategoryMove(BaseModel):
    """Schema for moving a category in the tree"""
    category_id: int = Field(..., gt=0)
    new_parent_id: Optional[int] = Field(None, gt=0)

    @validator('new_parent_id')
    def validate_move(cls, v, values):
        if 'category_id' in values and v == values['category_id']:
            raise ValueError('Category cannot be its own parent')
        return v

class CategoryBulkDelete(BaseModel):
    """Schema for bulk category deletion"""
    category_ids: List[int] = Field(..., min_items=1)
    move_children_to: Optional[int] = None  # Parent ID for orphaned children

    @validator('category_ids')
    def validate_ids(cls, v):
        if not all(x > 0 for x in v):
            raise ValueError('All category IDs must be positive integers')
        if len(v) != len(set(v)):
            raise ValueError('Duplicate category IDs are not allowed')
        return v

    @validator('move_children_to')
    def validate_move_to(cls, v, values):
        if v is not None:
            if v <= 0:
                raise ValueError('move_children_to must be a positive integer')
            if 'category_ids' in values and v in values['category_ids']:
                raise ValueError('Cannot move children to a category being deleted')
        return v