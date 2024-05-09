#!/usr/bin/env python
from __future__ import annotations

import concurrent.futures
import threading
import functools
import elasticapm
import time

from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, Future, as_completed
from concurrent.futures.process import BrokenProcessPool
from datemath import dm
from typing import Callable, Optional, TYPE_CHECKING

from assemblyline.common.isotime import epoch_to_iso, now_as_iso
from assemblyline.datastore.collection import Index
from assemblyline_core.server_base import ServerBase
from assemblyline_core.dispatching.dispatcher import BAD_SID_HASH
from assemblyline.common import forge
from assemblyline.common.metrics import MetricsFactory
from assemblyline.filestore import FileStore
from assemblyline.odm.messages.expiry_heartbeat import Metrics
from assemblyline.remote.datatypes import get_client
from assemblyline.remote.datatypes.set import Set

if TYPE_CHECKING:
    from assemblyline.datastore.collection import ESCollection


def file_delete_worker(logger, filestore_urls, file_batch) -> list[str]:
    try:
        filestore = FileStore(*filestore_urls)

        def filestore_delete(sha256: str) -> Optional[str]:
            filestore.delete(sha256)
            if not filestore.exists(sha256):
                return sha256
            return None

        return _file_delete_worker(logger, filestore_delete, file_batch)

    except Exception as error:
        logger.exception("Error in filestore worker: " + str(error))
    return []


def _file_delete_worker(logger, delete_action: Callable[[str], Optional[str]], file_batch) -> list[str]:
    finished_files: list[str] = []
    try:
        futures = []

        with ThreadPoolExecutor(8) as pool:
            for filename in file_batch:
                futures.append(pool.submit(delete_action, filename))

            for future in as_completed(futures):
                try:
                    erased_name = future.result()
                    if erased_name:
                        finished_files.append(erased_name)
                except Exception as error:
                    logger.exception("Error in filestore worker: " + str(error))

    except Exception as error:
        logger.exception("Error in filestore worker: " + str(error))
    return finished_files


class ExpiryManager(ServerBase):
    def __init__(self, redis_persist=None):
        self.config = forge.get_config()

        super().__init__('assemblyline.expiry', shutdown_timeout=self.config.core.expiry.sleep_time + 5)
        self.datastore = forge.get_datastore(config=self.config)
        self.filestore = forge.get_filestore(config=self.config)
        self.classification = forge.get_classification()
        self.expirable_collections: list[ESCollection] = []
        self.counter = MetricsFactory('expiry', Metrics)
        self.file_delete_worker = ProcessPoolExecutor(self.config.core.expiry.delete_workers)
        self.same_storage = self.config.filestore.storage == self.config.filestore.archive
        self.current_submission_cleanup = set()

        self.redis_persist = redis_persist or get_client(
            host=self.config.core.redis.persistent.host,
            port=self.config.core.redis.persistent.port,
            private=False,
        )
        self.redis_bad_sids = Set(BAD_SID_HASH, host=self.redis_persist)

        self.fs_hashmap = {
            'file': self.filestore_delete,
            'cached_file': self.cachestore_delete
        }

        for name, definition in self.datastore.ds.get_models().items():
            if hasattr(definition, 'expiry_ts'):
                self.expirable_collections.append(getattr(self.datastore, name))

        if self.config.core.metrics.apm_server.server_url is not None:
            self.log.info(f"Exporting application metrics to: {self.config.core.metrics.apm_server.server_url}")
            elasticapm.instrument()
            self.apm_client = forge.get_apm_client("expiry")
        else:
            self.apm_client = None

    @property
    def expiry_size(self):
        return self.config.core.expiry.delete_batch_size

    def stop(self):
        if self.counter:
            self.counter.stop()

        if self.apm_client:
            elasticapm.uninstrument()
        super().stop()

    def log_errors(self, function):
        @functools.wraps(function)
        def _func(*args, **kwargs):
            try:
                function(*args, **kwargs)
            except Exception:
                self.log.exception("Error in expiry worker")
        return _func

    def filestore_delete(self, file_batch, _):
        return self.file_delete_worker.submit(file_delete_worker, logger=self.log,
                                              filestore_urls=list(self.config.filestore.storage),
                                              file_batch=file_batch)

    def cachestore_delete(self, file_batch, _):
        return self.file_delete_worker.submit(file_delete_worker, logger=self.log,
                                              filestore_urls=list(self.config.filestore.cache),
                                              file_batch=file_batch)

    def _finish_delete(self, collection: ESCollection, task: Future, expire_only: list[str]):
        # Wait until the worker process finishes deleting files
        file_list: list[str] = []
        while self.running:
            self.heartbeat()
            try:
                file_list = task.result(5)
                break
            except concurrent.futures.TimeoutError:
                pass

        file_list.extend(expire_only)

        # build a batch delete job for all the removed files
        bulk = collection.get_bulk_plan()
        for sha256 in file_list:
            bulk.add_delete_operation(sha256)

        if len(file_list) > 0:
            self.log.info(f'[{collection.name}] Deleted associated files from the '
                          f'{"cachestore" if "cache" in collection.name else "filestore"}...')
            collection.bulk(bulk)
            self.counter.increment(f'{collection.name}', increment_by=len(file_list))
            self.log.info(f"[{collection.name}] Deleted {len(file_list)} items from the datastore...")
        else:
            self.log.warning(f'[{collection.name}] Expiry unable to clean up any of the files in filestore.')

    def _simple_delete(self, collection, delete_query, number_to_delete):
        self.heartbeat()
        collection.delete_by_query(delete_query)
        self.counter.increment(f'{collection.name}', increment_by=number_to_delete)
        self.log.info(f"[{collection.name}] Deleted {number_to_delete} items from the datastore...")

    def _cleanup_canceled_submission(self, sid):
        # Allowing us at minimum 5 minutes to cleanup the submission
        self.heartbeat(int(time.time() + 5 * 60))
        if self.apm_client:
            self.apm_client.begin_transaction("Delete canceled submissions")

        # Cleaning up the submission
        self.log.info(f"[submission] Deleting incomplete submission {sid}...")
        self.datastore.delete_submission_tree_bulk(sid, self.classification, transport=self.filestore)
        self.redis_bad_sids.remove(sid)

        # We're done cleaning up the sid, mark it as done
        self.current_submission_cleanup.remove(sid)

        if self.apm_client:
            self.apm_client.end_transaction("canceled_submissions", 'deleted')

    def _process_chunk(self, collection: ESCollection, start, end, final_date, number_to_delete):
        # We assume that no records are ever inserted such that their expiry_ts is in the past.
        # We also assume that the `end` dates are also in the past.
        # As long as these two things are true, the set returned by this query should be consistent.
        # The one race condition is that a record might be refreshed while the file
        # blob would be deleted anyway, leaving a file record with no filestore object
        delete_query = f"expiry_ts:{{{start} TO {end}]"

        # check if we are dealing with an index that needs file cleanup
        if self.config.core.expiry.delete_storage and collection.name in self.fs_hashmap:
            # Delete associated files
            delete_objects: list[str] = []
            for item in collection.stream_search(delete_query, fl='id', as_obj=False):
                self.heartbeat()
                delete_objects.append(item['id'])

            # Filter archived documents if archive filestore is the same as the filestore
            expire_only = []
            if self.same_storage and self.config.datastore.archive.enabled and collection.name == 'file':
                archived_files = self.datastore.file.multiexists(delete_objects, index_type=Index.ARCHIVE)
                delete_objects = [k for k, v in archived_files.items() if not v]
                expire_only = [k for k, v in archived_files.items() if v]

            delete_tasks = self.fs_hashmap[collection.name](delete_objects, final_date)

            # Proceed with deletion, but only after all the scheduled deletes for this
            self.log.info(f"[{collection.name}] Scheduled {len(delete_objects)}/{number_to_delete} files to be removed")
            self._finish_delete(collection, delete_tasks, expire_only)

        else:
            # Proceed with deletion
            self._simple_delete(collection, delete_query, number_to_delete)

    def feed_expiry_jobs(self, collection, start, jobs: list[concurrent.futures.Future],
                         pool: ThreadPoolExecutor) -> tuple[str, bool]:
        _process_chunk = self.log_errors(self._process_chunk)
        number_to_delete = 0
        self.heartbeat()

        # Start of expiry transaction
        if self.apm_client:
            self.apm_client.begin_transaction("Delete expired documents")

        final_date = self._get_final_date()

        # Break down the expiry window into smaller chunks of data
        while len(jobs) < self.config.core.expiry.iteration_max_tasks:

            # Get the next chunk
            end, number_to_delete = self._get_next_chunk(collection, start, final_date)

            # Check if we got anything
            if number_to_delete == 0:
                break

            # Process the chunk in the threadpool
            jobs.append(pool.submit(_process_chunk, collection, start, end, final_date, number_to_delete))

            # Prepare for next chunk
            start = end

        # End of expiry transaction
        if self.apm_client:
            self.apm_client.end_transaction(collection.name, 'deleted')

        return start, number_to_delete < self.expiry_size

    def _get_final_date(self):
        now = now_as_iso()
        if self.config.core.expiry.batch_delete:
            final_date = dm(f"{now}||-{self.config.core.expiry.delay}h/d").float_timestamp
        else:
            final_date = dm(f"{now}||-{self.config.core.expiry.delay}h").float_timestamp
        return epoch_to_iso(final_date)

    def _get_next_chunk(self, collection: ESCollection, start, final_date):
        """Find date of item at chunk size and the number of items that
           will be affected in between start date and the date found"""
        rows = collection.search(f"expiry_ts: {{{start} TO {final_date}]", rows=1,
                                 offset=self.expiry_size - 1, sort='expiry_ts asc',
                                 as_obj=False, fl='expiry_ts')
        if rows['items']:
            return rows['items'][0]['expiry_ts'], self.expiry_size
        return final_date, rows['total']

    def try_run(self):
        pool = ThreadPoolExecutor(self.config.core.expiry.workers)
        main_threads = []

        # Launch a thread that will expire submissions that have been deleted
        thread = threading.Thread(target=self.clean_deleted_submissions, args=[pool])
        thread.start()
        main_threads.append(thread)

        # Launch threads that expire data from each collection of data
        for collection in self.expirable_collections:
            thread = threading.Thread(target=self.run_collection, args=[pool, collection])
            thread.start()
            main_threads.append(thread)

        # Wait for all the threads to exit
        for thread in main_threads:
            thread.join()

    def clean_deleted_submissions(self, pool):
        """Delete canceled submissions"""
        while self.running:
            # Make sure we're not dedicating more then a quarter of the pool to this operation because it is costly
            for submission in self.datastore.submission.search(
                    "to_be_deleted:true", fl="sid", rows=max(1, int(self.config.core.expiry.workers / 4)))['items']:
                if submission.sid not in self.current_submission_cleanup:
                    self.current_submission_cleanup.add(submission.sid)
                    pool.submit(self.log_errors(self._cleanup_canceled_submission), submission.sid)
            self.sleep_with_heartbeat(self.config.core.expiry.sleep_time)

    def run_collection(self, pool: concurrent.futures.ThreadPoolExecutor, collection):
        """Feed batches of jobs to delete to the thread pool for the given collection."""
        start = "*"
        jobs: list[concurrent.futures.Future] = []

        while self.running:
            try:
                try:
                    # Fill up 'jobs' with tasks that have been sent to the thread pool
                    # 'jobs' may already have items in it, but 'start' makes sure the new
                    # task added starts where the last finshed
                    start, final_job_small = self.feed_expiry_jobs(collection, start, jobs, pool)

                    # Wait until some of our work finishes and there is room in the queue for more work
                    finished, _jobs = concurrent.futures.wait(jobs, return_when=concurrent.futures.FIRST_COMPLETED)
                    jobs = list(_jobs)
                    for job in finished:
                        job.result()

                    # If we have expired all the data reset the start pointer
                    if len(jobs) == 0:
                        start = '*'

                except Exception as e:
                    self.log.exception(str(e))
                    continue

                # IF the most recent job added to the jobs list is short then
                # all the data is currently queued up to delete and we can sleep
                if final_job_small:
                    self.sleep_with_heartbeat(self.config.core.expiry.sleep_time)

            except BrokenProcessPool:
                self.log.error("File delete worker pool crashed.")
                self.file_delete_worker = ProcessPoolExecutor(self.config.core.expiry.delete_workers)


if __name__ == "__main__":
    with ExpiryManager() as em:
        em.serve_forever()
