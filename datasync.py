# Accepts non-critical data from main signup application.

import datetime
import json
import logging

from google.appengine.api import urlfetch
from google.appengine.api import datastore
from google.appengine.ext import db
from google.appengine.ext import webapp

from config import Config
from membership import Membership

# Datastore model to keep track of DataSync information.
class SyncRunInfo(db.Model):
  run_times = db.IntegerProperty(default = 0)
  # The most recent cursor.
  cursor = db.StringProperty();
  # The last time we ran this successfully.
  last_run = db.DateTimeProperty()

# Handler for syncing data between dev and production apps.
class DataSyncHandler(webapp.RequestHandler):
  dev_url = "http://signup-dev.appspot.com/_datasync"
  time_format = "%Y %B %d %H %M %S"
  # The size of a batch for __batch_loop.
  batch_size = 10

  def get(self):
    config = Config()
    if not config.is_dev:
      # If we're production, send out new models.
      if ("X-Appengine-Cron" in self.request.headers.keys()) and \
          (self.request.headers["X-Appengine-Cron"]):
        # Only do this if a cron job told us to.
        run_info = SyncRunInfo.all().get()
        if not run_info:
          run_info = SyncRunInfo()
          run_info.put()

        if run_info.run_times == 0:
          # This is the first run. Sync everything.
          logging.info("First run, syncing everything...")
          self.__batch_loop(run_info.cursor)
        else:
          # Check for entries that changed since we last ran this.
          last_run = run_info.last_run
          logging.info("Last successful run: " + str(last_run))
          self.__batch_loop(run_info.cursor, "updated >", last_run)
        
        # Update the number of times we've run this.
        run_info = SyncRunInfo().all().get()
        run_info.run_times = run_info.run_times + 1
        # Clear the cursor property if we synced successfully.
        run_info.cursor = None
        logging.info("Ran sync %d time(s)." % (run_info.run_times))
        # Update the time of the last successful run.
        run_info.last_run = datetime.datetime.now()
        run_info.put()

      else:
        self.response.out.write("<h4>Only cron jobs can do that!</h4>")
        logging.info("Got GET request from non-cron job.")

  def __batch_loop(self, cursor = None, *args, **kwargs):
    cursor = cursor
    while True:
      if (args == () and kwargs == {}):
        query = Membership.all()
      else:
        query = Membership.all().filter(*args, **kwargs)
      query.with_cursor(start_cursor = cursor)
      members = query.fetch(self.batch_size)
    
      if len(members) == 0:
        break
      for member in members:
        member = self.__strip_sensitive(member)
        self.__post_member(member)
     
      cursor = query.cursor()
      run_info = SyncRunInfo.all().get()
      run_info.cursor = cursor
      run_info.put()

  # Posts member data to dev application.
  def __post_member(self, member):
    data = db.to_dict(member)
    # Convert datetimes to strings.
    for key in data.keys():
      if hasattr(data[key], "strftime"):
        data[key] = data[key].strftime(self.time_format)
    data = json.dumps(data)
    
    logging.debug("Posting entry: " + data)
    response = urlfetch.fetch(url = self.dev_url, payload = data,
        method = urlfetch.POST,
        headers = {"Content-Type": "application/json"})
    if response.status_code != 200:
      logging.error("POST received status code %d!" % (response.status_code))
      raise RuntimeError("POST failed. Check your quotas.")


  # Removes sensitive data from membership instances.
  def __strip_sensitive(self, member):
    member.spreedly_token = None
    member.hash = None
    return member
  
  def post(self):
    if Config.is_dev:
      # Only allow this if it's the dev server.
      entry = self.request.body
      logging.debug("Got new entry: " + entry)
      entry = json.loads(entry)
      # Change formatted date back into datetime.
      for key in entry.keys():
        if type(getattr(Membership, key)) == db.DateTimeProperty:
          entry[key] = datetime.datetime.strptime(entry[key], self.time_format)
      # entry should have everything nicely in a dict...
      member = Membership(**entry)

      # Is this an update or a new model?
      match = Membership.all().filter("email =", member.email).get()
      if match:
        # Replace the old one.
        logging.debug("Found entry with same username. Replacing...")
        db.delete(match)

      member.put()
      logging.debug("Put entry in datastore.")

app = webapp.WSGIApplication([
    ("/_datasync", DataSyncHandler),
    ], debug = True)
