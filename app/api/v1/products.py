from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import or_, and_
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_active_user, get_current_admin_user
from app.schemas.product import (
    ProductResponse,
    ProductFilter,
    ProductSearch,
    ProductListResponse,
)
from app.schemas.category import (
    CategoryResponse,
    CategoryTreeResponse,
    CategoryTreeNode,
    CategoryListResponse,
)
from app.schemas.common import APIResponse, PaginationParams
from app.models.product import Product
from app.models.category import Category
from app.models.user import User
from app.services.redis import RedisService

router = APIRouter(tags=["catalog"])

@router.get("/categories", response_model=APIResponse[CategoryListResponse])
async def list_categories(
    pagination: PaginationParams = Depends(),
    parent_id: Optional[int] = None,
    db: Session = Depends(get_db),
):

    query = db.query(Category)
    if parent_id is not None:
        query = query.filter(Category.parent_id == parent_id)

    total = query.count()
    categories = (
        query.offset((pagination.page - 1) * pagination.size)
        .limit(pagination.size)
        .all()
    )

    return APIResponse.success_response(CategoryListResponse(
        items=[CategoryResponse.from_orm(cat) for cat in categories],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size
    ))

@router.get("/categories/tree", response_model=APIResponse[CategoryTreeResponse])
async def get_category_tree(
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):

    cached_tree = redis.get_cached_category_tree()
    if cached_tree:
        return APIResponse.success_response(cached_tree)


    categories = db.query(Category).all()
    
    if not categories:

        response = CategoryTreeResponse(
            tree=[],
            total_categories=0,
            max_depth=0
        )
    else:
        root_categories = [c for c in categories if c.parent_id is None]
        
        def build_tree(category: Category) -> CategoryTreeNode:
            children = [c for c in categories if c.parent_id == category.id]
            return CategoryTreeNode(
                **CategoryResponse.from_orm(category).model_dump(),
                children=[build_tree(child) for child in children]
            )

        tree = [build_tree(root) for root in root_categories]
        max_depth = max(c.level for c in categories)


        response_data = {
            'tree': tree,
            'total_categories': len(categories),
            'max_depth': max_depth
        }
        

        response = CategoryTreeResponse(**response_data)


    redis.cache_category_tree(response)

    return APIResponse.success_response(response)

@router.get("/categories/{category_id}", response_model=APIResponse[CategoryResponse])
async def get_category(
    category_id: int,
    db: Session = Depends(get_db),
):

    category = db.query(Category).filter(Category.id == category_id).first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    return APIResponse.success_response(CategoryResponse.from_orm(category))

@router.get("/products", response_model=APIResponse[ProductListResponse])
async def list_products(
    filter_params: ProductFilter = Depends(),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):

    query = db.query(Product)


    if filter_params.category_id:
        query = query.filter(Product.category_id == filter_params.category_id)
    if filter_params.min_price is not None:
        query = query.filter(Product.price >= filter_params.min_price)
    if filter_params.max_price is not None:
        query = query.filter(Product.price <= filter_params.max_price)
    if filter_params.in_stock:
        query = query.filter(Product.stock_quantity > 0)
    if filter_params.characteristics:
        for key, value in filter_params.characteristics.items():
            query = query.filter(Product.characteristics[key].astext == str(value))
    if filter_params.search_query:
        query = query.filter(
            or_(
                Product.name.ilike(f"%{filter_params.search_query}%"),
                Product.description.ilike(f"%{filter_params.search_query}%")
            )
        )


    if filter_params.sort_by:
        field, order = filter_params.sort_by.split('_')
        column = getattr(Product, field)
        query = query.order_by(column.desc() if order == 'desc' else column.asc())

    total = query.count()
    products = (
        query.offset((filter_params.page - 1) * filter_params.size)
        .limit(filter_params.size)
        .all()
    )

    return APIResponse.success_response(ProductListResponse(
        items=[ProductResponse.from_orm(p) for p in products],
        total=total,
        page=filter_params.page,
        size=filter_params.size,
        has_more=total > filter_params.page * filter_params.size
    ))

@router.get("/products/{product_id}", response_model=APIResponse[ProductResponse])
async def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):

    cached_product = redis.get_cached_product(product_id)
    if cached_product:
        return APIResponse.success_response(cached_product)

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )

    response = ProductResponse.from_orm(product)
    

    redis.cache_product(product_id, response.dict())

    return APIResponse.success_response(response)

@router.get("/products/search", response_model=APIResponse[ProductListResponse])
async def search_products(
    search: ProductSearch = Depends(),
    db: Session = Depends(get_db),
):

    query = db.query(Product).filter(
        or_(
            Product.name.ilike(f"%{search.query}%"),
            Product.description.ilike(f"%{search.query}%")
        )
    )

    if search.category_id:
        query = query.filter(Product.category_id == search.category_id)

    total = query.count()
    products = (
        query.offset((search.page - 1) * search.size)
        .limit(search.size)
        .all()
    )

    return APIResponse.success_response(ProductListResponse(
        items=[ProductResponse.from_orm(p) for p in products],
        total=total,
        page=search.page,
        size=search.size,
        has_more=total > search.page * search.size
    ))