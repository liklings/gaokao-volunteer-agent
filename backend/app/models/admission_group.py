from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AdmissionGroup(Base):
    """
    专业组。同样用字符串短代号做主键（如 'seu-cs-04'），
    对应种子数据里的 group_id —— 官方"专业组编号"本身逐年会变，
    不适合当稳定标识，所以那个编号被放进了 AdmissionRecord.group_name（按年记录），
    而不是这里。
    """
    __tablename__ = "admission_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)

    code: Mapped[str | None] = mapped_column(String(16))  # 招生代码，多年通常稳定
    track: Mapped[str] = mapped_column(String(16), nullable=False)  # 物理类 / 历史类
    subjects_required: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    major_keywords: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    tuition: Mapped[int | None] = mapped_column(Integer)  # 元/年
    duration: Mapped[str | None] = mapped_column(String(8))

    # 江苏"专家版"数据源里"计划类别"这一列的原始值，比如"本科院校"/"专科院校"/
    # "提前录取本科院校—军队院校"/"提前录取本科院校—公安政法院校(专业)"/
    # "专科院校-定向培养军士"/"提前录取本科院校—高校专项计划"等——这是官方
    # 数据里现成的、权威的招生类型分类，不是靠猜的。rules_engine.py 用这个
    # 字段判断是不是"军事/警务/专项计划/定向培养"这类需要额外资格（政治
    # 审查/体检/户籍限制等）的特殊类型招生，比之前单纯拿学校名/专业组名
    # 做关键词匹配（app/services/special_admission.py）可靠得多。为 None 时
    # （老数据源、种子数据没有这一列）退回关键词匹配兜底，不当成"一定正常"。
    admission_category: Mapped[str | None] = mapped_column(String(64))

    why_recommend: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    caution: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    source_label: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    school: Mapped["School"] = relationship(back_populates="admission_groups")
    records: Mapped[list["AdmissionRecord"]] = relationship(
        back_populates="group", cascade="all, delete-orphan", order_by="AdmissionRecord.year.desc()"
    )
