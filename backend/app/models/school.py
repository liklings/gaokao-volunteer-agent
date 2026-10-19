from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class School(Base):
    """
    院校主表。主键用字符串短代号（如 'seu'），而不是自增整数——
    这样种子数据、导入脚本、前端引用都能用一个稳定、可读的 id，
    不用先插入拿到自增 id 再回填外键。
    """
    __tablename__ = "schools"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    city: Mapped[str] = mapped_column(String(32), nullable=False)
    province: Mapped[str] = mapped_column(String(16), nullable=False)
    # 江苏"专家版"数据源里"院校标签"这一列拆出来的标签（比如
    # "985"/"211"/"双一流"/"国重点"/"保研资格"/"本科"/"专科"），是数据源
    # 自带的、跟这所学校本身绑定的权威信息——不是靠学校名去匹配另一份
    # 参考表猜出来的。app/services/school_quality.py 现在优先用这个字段
    # 判断学校档次，比之前那套"名字匹配《全国高等院校信息汇总》静态表"
    # 可靠得多（同一行数据，不存在匹配不上的问题）。
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    # "院校排名"这一列——数据源自带的国内综合排名，越小越靠前，查不到
    # 时 None。同样优先于 school_quality.py 静态表里的排名数据。
    overall_rank: Mapped[int | None] = mapped_column(Integer)

    admission_groups: Mapped[list["AdmissionGroup"]] = relationship(
        back_populates="school", cascade="all, delete-orphan"
    )
