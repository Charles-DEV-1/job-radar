begin;

-- The private Stage 4 bucket also stores the reviewed master JSON and layout
-- metadata used by scheduled tailoring runs.
update storage.buckets
set allowed_mime_types = array[
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/pdf',
    'application/json'
]
where id = 'radar-resumes' and public = false;

commit;
