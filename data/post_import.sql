-- 导入数据后执行：数据里带显式 id，需要把自增序列对齐到最大 id，否则之后新增记录会主键冲突
select setval(pg_get_serial_sequence('admission_records','id'), (select coalesce(max(id),1) from admission_records));
select setval(pg_get_serial_sequence('score_rank_rows','id'), (select coalesce(max(id),1) from score_rank_rows));
select setval(pg_get_serial_sequence('policy_documents','id'), (select coalesce(max(id),1) from policy_documents));
select setval(pg_get_serial_sequence('policy_chunks','id'), (select coalesce(max(id),1) from policy_chunks));
