import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text
from app.db.database import get_db_session, engine

async def run_migration():
    print("Connecting to database...")
    try:
        async with get_db_session() as session:
            # Check table existence or create directly
            print("Creating question_paper_caches table...")
            create_table_ddl = """
            CREATE TABLE IF NOT EXISTS question_paper_caches (
                id SERIAL PRIMARY KEY,
                subject_code VARCHAR(50) NOT NULL,
                academic_year VARCHAR(50) NOT NULL,
                exam_type VARCHAR(20) NOT NULL,
                portal_postback_target VARCHAR(500) NOT NULL,
                telegram_file_id VARCHAR(500) NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
            );
            """
            
            create_index_ddl = """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_qp_cache_lookup 
            ON question_paper_caches (subject_code, academic_year, exam_type);
            """
            
            create_code_idx_ddl = """
            CREATE INDEX IF NOT EXISTS idx_qp_cache_code 
            ON question_paper_caches (subject_code);
            """
            
            await session.execute(text(create_table_ddl))
            await session.execute(text(create_index_ddl))
            await session.execute(text(create_code_idx_ddl))
            
            print("Applying updates for SyncState metrics column and performance indexes...")
            alter_sync_states_ddl = "ALTER TABLE sync_states ADD COLUMN IF NOT EXISTS last_metrics JSONB NULL;"
            idx_events_created_at_ddl = "CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at);"
            idx_events_event_type_ddl = "CREATE INDEX IF NOT EXISTS idx_events_event_type ON events (event_type);"
            idx_inbox_portal_msg_id_ddl = "CREATE INDEX IF NOT EXISTS idx_inbox_portal_msg_id ON inbox_messages (portal_message_id);"
            
            await session.execute(text(alter_sync_states_ddl))
            await session.execute(text(idx_events_created_at_ddl))
            await session.execute(text(idx_events_event_type_ddl))
            await session.execute(text(idx_inbox_portal_msg_id_ddl))
            
            await session.commit()
            print("Database migration completed successfully!")
            
    except Exception as e:
        print(f"Migration failed: {e}")
        # Try direct sqlite metadata bindings as fallback if using SQLite in test environments
        try:
            from app.db.models import Base
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            print("Fallback Base.metadata.create_all succeeded!")
        except Exception as fb_err:
            print(f"Fallback migration also failed: {fb_err}")
            sys.exit(1)
            
    finally:
        await engine.dispose()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_migration())
