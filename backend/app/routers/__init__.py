"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

配载批次回执使用独立路由，先于 /api/{module} 注册，避免动态段抢先匹配。
"""
from __future__ import annotations

from app.routers import resource, stowage

ROUTERS = [stowage, resource]
