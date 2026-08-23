import asyncio
from app.db.database import async_session_factory
from sqlalchemy import text

async def export():
    async with async_session_factory() as session:
        # Question papers
        res = await session.execute(text('SELECT subject_code, academic_year, exam_type, portal_postback_target, telegram_file_id, status, file_kind, file_size_bytes FROM question_paper_caches WHERE telegram_file_id IS NOT NULL'))
        rows = res.fetchall()
        
        sql_lines = ['BEGIN;']
        for r in rows:
            code = r[0].replace("'", "''")
            year = r[1].replace("'", "''")
            etype = r[2].replace("'", "''")
            target = r[3].replace("'", "''")
            fid = r[4].replace("'", "''")
            status = r[5].replace("'", "''")
            kind = f"'{r[6]}'" if r[6] else "NULL"
            size = r[7] if r[7] is not None else "NULL"
            sql_lines.append(f"INSERT INTO question_paper_caches (subject_code, academic_year, exam_type, portal_postback_target, telegram_file_id, status, file_kind, file_size_bytes, created_at, updated_at) VALUES ('{code}', '{year}', '{etype}', '{target}', '{fid}', '{status}', {kind}, {size}, NOW(), NOW()) ON CONFLICT (subject_code, academic_year, exam_type) DO UPDATE SET telegram_file_id = EXCLUDED.telegram_file_id, status = EXCLUDED.status;")
        
        # Attachments
        att_res = await session.execute(text('SELECT attachment_path, portal_filename, telegram_file_id, file_kind, file_size_bytes, status FROM attachment_caches WHERE telegram_file_id IS NOT NULL'))
        att_rows = att_res.fetchall()
        for r in att_rows:
            path = r[0].replace("'", "''")
            esc_fname = r[1].replace("'", "''") if r[1] else ""
            fname = f"'{esc_fname}'" if r[1] else "NULL"
            fid = r[2].replace("'", "''")
            kind = f"'{r[3]}'" if r[3] else "NULL"
            size = r[4] if r[4] is not None else "NULL"
            status = r[5].replace("'", "''")
            sql_lines.append(f"INSERT INTO attachment_caches (attachment_path, portal_filename, telegram_file_id, file_kind, file_size_bytes, status, created_at, updated_at) VALUES ('{path}', {fname}, '{fid}', {kind}, {size}, '{status}', NOW(), NOW()) ON CONFLICT (attachment_path) DO UPDATE SET telegram_file_id = EXCLUDED.telegram_file_id, status = EXCLUDED.status;")
        
        sql_lines.append('COMMIT;')
        
        with open('sync_cache.sql', 'w', encoding='utf-8') as f:
            f.write('\n'.join(sql_lines))
        print(f"Successfully generated sync_cache.sql with {len(rows)} Question Papers and {len(att_rows)} Attachments.")

if __name__ == '__main__':
    asyncio.run(export())
