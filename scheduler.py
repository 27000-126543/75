import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from database import SessionLocal
from config import settings
from routers.reports import _generate_daily_report
from models import DailyReport
from services.notification_service import push_system_notification

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def daily_report_job():
    db = SessionLocal()
    try:
        yesterday = datetime.utcnow() - timedelta(days=1)
        start_of_day = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

        existing = db.query(DailyReport).filter(DailyReport.report_date == start_of_day).first()
        if existing:
            logger.info(f"日报已存在: {yesterday.strftime('%Y-%m-%d')}")
            return

        report = _generate_daily_report(db, yesterday)
        db.add(report)
        db.commit()
        db.refresh(report)

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(push_system_notification(
                db,
                f"矿山日报已生成: {yesterday.strftime('%Y-%m-%d')}",
                f"总产量{report.total_output}吨, 设备利用率{report.equipment_utilization}%, 安全事件{report.safety_event_count}起"
            ))
        finally:
            loop.close()

        logger.info(f"日报生成成功: {yesterday.strftime('%Y-%m-%d')}")
    except Exception as e:
        logger.error(f"日报生成失败: {e}")
    finally:
        db.close()


def start_scheduler():
    scheduler.add_job(
        daily_report_job,
        CronTrigger(
            hour=settings.SCHEDULER_DAILY_REPORT_HOUR,
            minute=settings.SCHEDULER_DAILY_REPORT_MINUTE
        ),
        id="daily_report",
        replace_existing=True
    )
    scheduler.start()
    logger.info("调度器已启动，每日定时任务已注册")


def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("调度器已停止")
