from datetime import datetime
from typing import Optional
from sqlmodel import Field, SQLModel

AGENT_SEQUENCE = [
    "brand_basics",
    "content_catalog",
    "performance_ads",
    "geo_visibility",
    "store_cro",
    "research",
    "social_profile",
    "social_media_audit",
]


class AuditRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    url: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "pending"           # pending | running | complete | failed
    current_agent: Optional[str] = None
    progress_pct: int = 0             # 0-100
    brand_basics: Optional[str] = None      # JSON blob
    content_catalog: Optional[str] = None
    performance_ads: Optional[str] = None
    geo_visibility: Optional[str] = None
    store_cro: Optional[str] = None
    research: Optional[str] = None
    social_profile: Optional[str] = None
    social_media_audit: Optional[str] = None
    error: Optional[str] = None
    report_html: Optional[str] = None
    share_token: Optional[str] = None
    one_thing: Optional[str] = None
    monitoring: bool = False
    changes_summary: Optional[str] = None   # JSON — LLM-generated change diff
    roadmap_json: Optional[str] = None      # JSON — 30-day action roadmap
    analyst_brief_json: Optional[str] = None  # JSON — analyst brief + verdict
    cross_findings_json: Optional[str] = None  # JSON — cross-agent pattern list
    agentic_meta_json: Optional[str] = None   # JSON — WorkingMemory.to_report_dict()


class CompareRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    url_a: str
    url_b: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "pending"            # pending | running | complete | failed
    audit_id_a: Optional[int] = None
    audit_id_b: Optional[int] = None
    cache_hit_a: bool = False
    cache_hit_b: bool = False
    compare_html: Optional[str] = None
    findings_json: Optional[str] = None
    error: Optional[str] = None
    compare_share_token: Optional[str] = None
    swot_json: Optional[str] = None
    strategy_json_a: Optional[str] = None
    strategy_json_b: Optional[str] = None


class ScoreHistory(SQLModel, table=True):
    """One row per completed audit — stores native-unit scores for trend tracking."""
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_url: str
    audit_id: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    brand_basics_score: Optional[float] = None   # synthesised 0-10
    content_score: Optional[float] = None        # pdp_quality_score  0-10
    ads_score: Optional[float] = None            # hook_strength_score 0-10
    geo_score: Optional[float] = None            # raw geo_score       0-100
    store_score: Optional[float] = None          # mobile pagespeed    0-100
    research_score: Optional[float] = None       # synthesised 0-10
    overall_score: Optional[float] = None        # _overall_health     0-100


class ViralityRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    url: str
    product_name: str
    description: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "pending"           # pending | running | complete | failed
    score: Optional[int] = None
    result: Optional[str] = None      # JSON blob
    error: Optional[str] = None


class BrandConnector(SQLModel, table=True):
    """Stores third-party API tokens per brand URL for deep private data access."""
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_url: str = Field(index=True)
    shopify_token: Optional[str] = None       # Shopify Admin API access token
    shopify_store_url: Optional[str] = None   # e.g. https://mystore.myshopify.com
    meta_token: Optional[str] = None          # Meta Marketing API access token
    meta_account_id: Optional[str] = None     # e.g. act_1234567890
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
