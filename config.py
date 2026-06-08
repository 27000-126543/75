from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "智慧矿山安全生产与智能调度系统"
    API_PREFIX: str = "/api/v1"
    DATABASE_URL: str = "sqlite:///./mine_safety.db"

    ALCOHOL_THRESHOLD: float = 0.02
    VIBRATION_THRESHOLD: float = 4.5
    TEMPERATURE_THRESHOLD: float = 85.0
    GAS_THRESHOLD: float = 1.0
    DUST_THRESHOLD: float = 10.0
    ORE_GRADE_THRESHOLD: float = 45.0
    INVENTORY_SAFETY_RATIO: float = 0.3

    SCHEDULER_DAILY_REPORT_HOUR: int = 0
    SCHEDULER_DAILY_REPORT_MINUTE: int = 30

    SECRET_KEY: str = "mine-safety-secret-key-2024"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    class Config:
        env_file = ".env"


settings = Settings()
