import gzip

from .config import config
from .session import Session
from .util import (db_session, fetch_from_queue, NoItemsInQueue, cases_batch,
                   cases_batch_filter, process_cases, get_detail_loc)
from .models import ScrapeVersion, Scrape, Case
from sqlalchemy import and_,not_
from hashlib import sha256
import requests
import boto3
import re
import json
import os
from datetime import datetime
import time
from queue import Queue
import concurrent.futures

MJCS_AUTH_TARGET = 'http://casesearch.courts.state.md.us/casesearch/inquiry-index.jsp'

class ScraperItem:
    def __init__(self, case_number, detail_loc):
        self.case_number = case_number
        self.detail_loc = detail_loc
        self.timeouts = 0
        self.errors = 0

class FailedScrape(Exception):
    pass

class FailedScrapeTimeout(FailedScrape):
    pass

class FailedScrape500(FailedScrape):
    pass

class FailedScrapeNotFound(FailedScrape):
    pass

class FailedScrapeTooShort(FailedScrape):
    pass

class FailedScrapeUnexpectedError(FailedScrape):
    pass

class FailedScrapeSearchResults(FailedScrape):
    pass

class FailedScrapeNoCaseNumber(FailedScrape):
    pass

class FailedScrapeUnknownError(FailedScrape):
    pass

class CompletedScrape(Exception):
    pass

class ExpiredSession(Exception):
    pass

class Scraper:
    def __init__(self, on_error=None, threads=1,casebucket=None, log_failed_scrapes=True, quiet=False,failbucket=None):
        self.on_error = on_error
        self.threads = threads
        self.log_failed_scrapes = log_failed_scrapes
        self.quiet = quiet
        self.case_bucket = casebucket
        self.fail_bucket = failbucket

    def print_details(self, msg):
        if not self.quiet or os.getenv('VERBOSE'):
            print(msg)

    def check_scrape_sanity(self, case_number, html):
        if "Acceptance of the following agreement is required" in html:
            raise ExpiredSession
        elif len(html) < 1000:
            raise FailedScrapeTooShort
        elif "An unexpected error occurred" in html:
            raise FailedScrapeUnexpectedError
        elif "Note: Initial Sort is by Last Name." in html:
            raise FailedScrapeSearchResults
        else:
            # case numbers will often be displayed with dashes and/or spaces between parts of it
            if re.search(r'[\- ]*'.join(case_number),html):
                return
            if re.search(r'[\- ]*'.join(case_number.lower()),html):
                return
            raise FailedScrapeNoCaseNumber

    def store_case_details(self, case_number, detail_loc, html, scrape_duration=None, check_before_store=True):
        if check_before_store:
            add = False
            try:
                previous_fetch = self.case_bucket[case_number]
            except KeyError:
                self.print_details("Case details for %s not found, adding..." % case_number)
                add = True
            else:
                if gzip.decompress(previous_fetch['Body']).decode() != html:
                    self.print_details("Found new version of case %s, replacing..." % case_number)
                    add = True
        else:
            add = True

        if add:
            timestamp = datetime.now()
            ver = sha256(html.encode('utf-8')).hexdigest()
            case = {'Body': gzip.compress(html.encode()), 'Metadata': {'timestamp': timestamp.isoformat(), 'detail_loc': detail_loc, 'version_id': ver}}
            self.case_bucket[case_number] = case
            self.case_bucket.commit()
            obj_version_id = case['Metadata']['version_id']+str(timestamp)
            with db_session() as db:
                scrape_version = ScrapeVersion(
                    s3_version_id = obj_version_id,
                    case_number = case_number,
                    length = len(html),
                    sha256 = ver
                )
                scrape = Scrape(
                    case_number = case_number,
                    s3_version_id = obj_version_id,
                    timestamp = timestamp,
                    duration = scrape_duration
                )
                db.add(scrape_version)
                db.flush() # to satisfy foreign key constraint of scrapes
                db.add(scrape)
                db.execute(
                    Case.__table__.update()\
                        .where(Case.case_number == case_number)\
                        .values(last_scrape = timestamp)
                )

    def log_failed_scrape(self, case_number, detail_loc, error):
        self.print_details("Failed to scrape case %s: %s" % (case_number, error))
        if self.log_failed_scrapes:
            self.fail_bucket[case_number] = error
            self.fail_bucket.commit()

    def handle_scrape_response(self, scraper_item, response, scrape_duration, check_before_store=True):
        if response.status_code != 200:
            if response.status_code == 500:
                scraper_item.errors += 1
                if scraper_item.errors >= config.QUERY_ERROR_LIMIT:
                    self.log_failed_scrape(scraper_item.case_number,
                        scraper_item.detail_loc, "Reached 500 error limit")
                    raise FailedScrape500
            # This is how requests deals with redirects
            elif response.status_code == 302 \
                    and response.headers['Location'] == MJCS_AUTH_TARGET:
                raise ExpiredSession
            else:
                scraper_item.errors += 1
                if scraper_item.errors >= config.QUERY_ERROR_LIMIT:
                    self.log_failed_scrape(
                        scraper_item.case_number, scraper_item.detail_loc,
                        "Received unexpected response: code = %d, body = %s" % (response.status_code, response.text)
                    )
                    raise FailedScrapeUnknownError
        elif re.search(r'<span class="error">\s*<br>CaseSearch will only display results',response.text):
            scraper_item.errors += 1
            #no need to hammer this one, if it shows up once, it will not be remediated.
            self.log_failed_scrape(scraper_item.case_number,scraper_item.detail_loc, "Case details not found")
            raise FailedScrapeNotFound
        elif 'Sorry, but your query has timed out after 2 minute' in response.text:
            scraper_item.timeouts += 1
            if scraper_item.timeouts >= config.QUERY_TIMEOUTS_LIMIT:
                self.log_failed_scrape(scraper_item.case_number,
                    scraper_item.detail_loc, "Reached timeout limit")
                raise FailedScrapeTimeout
        else:
            # Some sanity checks
            try:
                self.check_scrape_sanity(scraper_item.case_number, response.text)
            except FailedScrape as e:
                scraper_item.errors += 1
                #If it gets here, it will not be helped by rerunning.
                self.log_failed_scrape(scraper_item.case_number, scraper_item.detail_loc,str(type(e)))
                raise e
            else:
                self.store_case_details(scraper_item.case_number, scraper_item.detail_loc, response.text, scrape_duration, check_before_store)
                raise CompletedScrape

    def scrape_case(self, case_number, detail_loc, session=None, check_before_store=True):
        if not session:
            session = Session()
        scraper_item = ScraperItem(case_number, detail_loc)
        while True:
            try:
                self.print_details("Requesting case details for %s" % case_number)
                begin = datetime.now()
                response = session.post(
                    'http://casesearch.courts.state.md.us/casesearch/inquiryByCaseNum.jis',
                    data = {
                        'caseId': case_number
                    },
                    allow_redirects = False
                )
                end = datetime.now()
                duration = (begin - end).total_seconds()
            except requests.exceptions.Timeout:
                time.sleep(0.1) # courtesy
                scraper_item.timeouts += 1
                if scraper_item.timeouts >= config.QUERY_TIMEOUTS_LIMIT:
                    self.log_failed_scrape(case_number, detail_loc, "Reached timeout limit")
                    raise FailedScrapeTimeout
            else:
                try:
                    self.handle_scrape_response(scraper_item, response, duration, check_before_store)
                except ExpiredSession:
                    self.print_details("Renewing session")
                    session.renew()
                except CompletedScrape:
                    self.print_details("Completed scraping %s" % case_number)
                    break
                except Exception as e:
                    e.html = response.text
                    time.sleep(1) #anti hammer
                    raise e

    def scrape_specific_case(self, case_number):
        detail_loc = get_detail_loc(case_number)
        return self.scrape_case(case_number, detail_loc)

    def scrape_case_thread(self, case):
        case_number = case['case_number']
        detail_loc = case['detail_loc']
        session_pool = case['session_pool']
        check_before_store = case['check_before_store']

        session = session_pool.get()
        ret = None
        try:
            ret = self.scrape_case(case_number, detail_loc, session, check_before_store)
        except:
            raise
        finally:
            session_pool.put_nowait(session)
        return ret

    def scrape_from_queue(self, queue, nitems=None):
        session_pool = Queue(self.threads)
        for i in range(self.threads):
            session = Session()
            session.renew() # Renew session immediately bc it was causing errors in Lambda
            session_pool.put_nowait(session)
        counter = {
            'total': 0,
            'count': 0
        }

        while True:
            queue_items = fetch_from_queue(queue, nitems)
            if not queue_items:
                print("No items found in queue")
                break
            counter['total'] += len(queue_items)

            cases = []
            for item in queue_items:
                body = json.loads(item.body)
                case_number = body['case_number']
                detail_loc = body['detail_loc']
                cases.append({
                    'case_number': case_number,
                    'detail_loc': detail_loc,
                    'session_pool': session_pool,
                    'check_before_store': True,
                    'item': item
                })

            def queue_on_success(case):
                case['item'].delete()
            def queue_on_error(exception, case):
                if self.on_error:
                    action = self.on_error(exception, case['case_number'])
                    if action == 'delete' or action == 'store':
                        if action == 'store':
                            self.store_case_details(
                                case['case_number'],
                                case['detail_loc'],
                                exception.html
                            )
                        case['item'].delete()
                        return 'delete'
                    return action
                raise exception

            print("Scraping %d cases" % len(cases))
            process_cases(self.scrape_case_thread, cases, queue_on_success, queue_on_error, self.threads, counter)
            print("Finished scraping %d cases" % counter['total'])
            if nitems:
                break # don't need to keep looping

        print("Total number of scraped cases: %d" % counter['count'])
        if counter['count'] == 0:
            raise NoItemsInQueue

    def scrape_from_scraper_queue(self, nitems=None):
        return self.scrape_from_queue(config.scraper_queue, nitems)

    def scrape_from_failed_queue(self, nitems=None):
        self.log_failed_scrapes = False
        return self.scrape_from_queue(config.scraper_failed_queue, nitems)

    def scrape_missing_cases(self):
        session_pool = Queue(self.threads)
        fails = list(self.fail_bucket.keys())
        for i in range(self.threads):
            session = Session()
            session.renew() # Renew session immediately bc it was causing errors in Lambda
            session_pool.put_nowait(session)
        filter = and_(Case.last_scrape == None, Case.scrape_exempt != True,Case.case_number.notin_(fails))
        with db_session() as db:
            print('Getting count of unscraped cases...',end='',flush=True)
            counter = {
                'total': db.query(Case.case_number).filter(filter).count(),
                'count': 0
            }
            print('Done.')
            print('Generating batch queries...',end='',flush=True)
            batch_filters = cases_batch_filter(db, filter)
            print('Done.')

        for batch_filter in batch_filters:
            with db_session() as db:
                print('Fetching batch of cases...',end='',flush=True)
                cases = cases_batch(db, batch_filter)
                print('Done.')
            cases = [{
                'case_number': case.case_number,
                'detail_loc': case.detail_loc,
                'session_pool': session_pool,
                'check_before_store': False,
            } for case in cases]
            def _on_error(exception, case):
                if self.on_error:
                    action = self.on_error(exception, case['case_number'])
                    if action == 'delete' or action == 'store':
                        if action == 'store':
                            self.store_case_details(
                                case['case_number'],
                                case['detail_loc'],
                                exception.html,
                                check_before_store=False
                            )
                        return 'delete'
                    return action
                raise exception
            process_cases(self.scrape_case_thread, cases, None, _on_error, self.threads, counter)
