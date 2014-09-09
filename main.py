import wsgiref.handlers
import datetime, time, hashlib, urllib, urllib2, re, os
from google.appengine.api import app_identity
from google.appengine.ext import deferred
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.api import urlfetch, mail, memcache, users, taskqueue
from google.appengine.ext.webapp import template
import json
from cgi import escape
from pprint import pprint
from datetime import datetime, date, time

from config import Config
from membership import Membership
import logging
import spreedly
import keymaster
import base64
import sys

ORG_NAME = 'Hacker Dojo'
APP_NAME = app_identity.get_application_id()
EMAIL_FROM = "Dojo Signup <no-reply@%s.appspotmail.com>" % APP_NAME
EMAIL_FROM_AYST = "Billing System <robot@hackerdojo.com>"
DAYS_FOR_KEY = 0
INTERNAL_DEV_EMAIL = "Internal Dev <internal-dev@hackerdojo.com>"
DOMAIN_HOST = 'domain.hackerdojo.com'
DOMAIN_USER = 'api@hackerdojo.com'
SUCCESS_HTML_URL = 'http://hackerdojo.pbworks.com/api_v2/op/GetPage/page/SubscriptionSuccess/_type/html'
PAYPAL_EMAIL = 'PayPal <paypal@hackerdojo.com>'
APPS_DOMAIN = 'hackerdojo.com'
SIGNUP_HELP_EMAIL = 'signupops@hackerdojo.com'
TREASURER_EMAIL = 'treasurer@hackerdojo.com'
GOOGLE_ANALYTICS_ID = 'UA-11332872-2'

def fetch_usernames(use_cache=True):
    usernames = memcache.get('usernames')
    if usernames and use_cache:
        return usernames
    else:
        resp = urlfetch.fetch('http://%s/users' % DOMAIN_HOST, deadline=10)
        if resp.status_code == 200:
            usernames = [m.lower() for m in json.loads(resp.content)]
            if not memcache.set('usernames', usernames, 60*60*24):
                logging.error("Memcache set failed.")
            return usernames

def render(path, local_vars):
    c = Config()
    template_vars = {'is_prod': c.is_prod, 'org_name': ORG_NAME, 'analytics_id': GOOGLE_ANALYTICS_ID, 'domain': APPS_DOMAIN}
    template_vars.update(local_vars)

    if c.is_dev:
      template_vars["dev_message"] = "You are using the dev version of \
              Signup."
    return template.render(path, template_vars)

class UsedCode(db.Model):
    email = db.StringProperty()
    created = db.DateTimeProperty(auto_now_add=True)
    code = db.StringProperty()
    extra = db.StringProperty()
    completed = db.DateTimeProperty()

class RFIDSwipe(db.Model):
    username = db.StringProperty()
    rfid_tag = db.StringProperty()
    created = db.DateTimeProperty(auto_now_add=True)
    success = db.BooleanProperty()

class RFIDSwipeHandler(webapp.RequestHandler):
    def get(self):
        if self.request.get('maglock:key') != keymaster.get('maglock:key'):
            self.response.out.write("Access denied")
        else:
            rfid_tag = self.request.get('rfid_tag')
            if rfid_tag:
                m = Membership.all().filter('rfid_tag ==', rfid_tag).get()
                if m:
                  username = m.username
                  if "active" in m.status:
                     success = True
                  else:
                     success = False
                     subject = "Reactivate your RFID key now - renew your Hacker Dojo Subscription!"
                     body = """Hi %s,
                     \nIt looks like you just tried using your RFID key to open the doors to Hacker Dojo.
                     \nOne teeny tiny issue, it looks like your membership has lapsed!
                     \nThis can happen by mistake sometimes, so no worries at all.
                     \nThe good news is you can reactivate your membership with only a few clicks:
                     \n%s
                     \nWith warmest regards, \nThe Lobby Door
                     """ % (m.first_name,m.subscribe_url())
                     deferred.defer(mail.send_mail, sender="Maglock <robot@hackerdojo.com>", to=m.email,
                     subject=subject, body=body, _queue="emailthrottle")    
                else:
                  username = "unknown ("+rfid_tag+")"
                  success = False   
                rs = RFIDSwipe(username=username, rfid_tag=rfid_tag, success=success)
                rs.put()
                if "mark.hutsell" in username or "some.other.evilguy" in username:
                  deferred.defer(mail.send_mail, sender="Maglock <robot@hackerdojo.com>", to="Emergency Paging System <page@hackerdojo.com>",
                     subject="RFID Entry: " + username, body="Lobby entry", _queue="emailthrottle")
                  urlfetch.fetch("http://www.dustball.com/call/call.php?str=RFID+Entry+"+username)
            self.response.out.write("OK")

class BadgeChange(db.Model):
    created = db.DateTimeProperty(auto_now_add=True)
    rfid_tag = db.StringProperty()    
    username = db.StringProperty()
    description = db.StringProperty()

class MainHandler(webapp.RequestHandler):
    def get(self):
        #signup_users = Membership.all().fetch(10000)
        template_values = {
            'plan': self.request.get('plan', 'full'),
            'paypal': self.request.get('paypal')}
        self.response.out.write(render('templates/main.html', template_values))
    
    def post(self):
        refer = self.request.get('refer')
        first_name = self.request.get('first_name')
        last_name = self.request.get('last_name')
        twitter = self.request.get('twitter').lower().strip().strip('@')
        email = self.request.get('email').lower().strip()
        plan = self.request.get('plan', 'full')
        
        # See if the referring user is valid.
        try:
          ref_first_name = refer.split()[0]
          ref_last_name = refer.split()[1]
          referred_user = db.GqlQuery("SELECT * FROM Membership \
              WHERE first_name = :first_name AND last_name = :last_name",
              first_name = ref_first_name,
              last_name = ref_last_name).get()
        except IndexError:
          referred_user = None
        
        if not first_name or not last_name or not email:
            self.response.out.write(render('templates/main.html', {
                'plan': plan, 'message': "Sorry, we need name and e-mail address."}))
        elif (not referred_user and refer != ""):
          self.response.out.write(render('templates/main.html', {
            'plan': plan,
            'message': "The person who referred you is not an active user."}))
        else:
            
            # this just runs a check twice. (there is no OR in GQL)
            # first name, last name
            existing_member = db.GqlQuery("SELECT * FROM Membership WHERE first_name = :first_name AND last_name = :last_name", first_name=first_name, last_name=last_name).get()
            if existing_member:
                membership = existing_member
            # email
            existing_member = db.GqlQuery("SELECT * FROM Membership WHERE email = :email", email=email).get()
            if existing_member:
                membership = existing_member

            first_part = re.compile(r'[^\w]').sub('', first_name.split(' ')[0])
            last_part = re.compile(r'[^\w]').sub('', last_name)
            if len(first_part)+len(last_part) >= 15:
                last_part = last_part[0]
            username = '.'.join([first_part, last_part]).lower()
            if username in fetch_usernames():
                username = email.split('@')[0].lower()
            
            # username@hackerdojo.com
            existing_member = db.GqlQuery("SELECT * FROM Membership WHERE email = :email", email='%s@hackerdojo.com' % username).get()
            if existing_member:
                membership = existing_member
            
            try:
                membership
                if membership.extra_dnd == True:
                    self.response.out.write("Error #237.  Please contact signupops@hackerdojo.com")
                    return
                if membership.status == "suspended":
                    c = Config()
                    self.redirect(str("https://www.spreedly.com/%s/subscriber_accounts/%s" % (c.SPREEDLY_ACCOUNT, membership.spreedly_token)))              
            except NameError:
                membership = None

                
            # old code below
            #existing_member = Membership.get_by_email(email)
            #if existing_member and existing_member.status in [None, 'paypal']:
            #    existing_member.delete()
            if membership is None:
                if referred_user:
                  referuserid = referred_user.username
                else:
                  referuserid = None
                membership = Membership(
                    first_name=first_name, last_name=last_name, email=email,
                    plan=plan, twitter=twitter, referuserid=referuserid)
                if self.request.get('paypal') == '1':
                    membership.status = 'paypal'
                membership.hash = hashlib.md5(membership.email).hexdigest()
                if '1337' in self.request.get('referrer').upper():
                    membership.referrer = re.sub("[^0-9]", "", self.request.get('referrer').upper())
                else:
                    membership.referrer = self.request.get('referrer').replace('\n', ' ')
                membership.put()
            
            # if there is a membership, redirect here
            if membership.status != "active":
              #self.redirect(str('/account/%s' % membership.hash))
              # HRD compatible hack, code taken from AccountHandler::get()
              first_part = re.compile(r'[^\w]').sub('', membership.first_name.split(' ')[0]) # First word of first name
              last_part = re.compile(r'[^\w]').sub('', membership.last_name)
              if len(first_part)+len(last_part) >= 15:
                  last_part = last_part[0] # Just last initial
              username = '.'.join([first_part, last_part]).lower()
              if username in fetch_usernames():
                  username = membership.email.split('@')[0].lower()
              if self.request.get('u'):
                  pick_username = True
              message = escape(self.request.get('message'))
              account_url = str('/account/%s' % membership.hash)
              self.response.out.write(render('templates/account.html', locals()))
            else:
              self.response.out.write(render('templates/main.html',
                {'message': 'The Email address is registered in our system.'}))
class AccountHandler(webapp.RequestHandler):
    def get(self, hash):
        membership = Membership.get_by_hash(hash)
        if membership:
          # steal this part to detect if they registered with hacker dojo email above
          first_part = re.compile(r'[^\w]').sub('', membership.first_name.split(' ')[0]) # First word of first name
          last_part = re.compile(r'[^\w]').sub('', membership.last_name)
          if len(first_part)+len(last_part) >= 15:
              last_part = last_part[0] # Just last initial
          username = '.'.join([first_part, last_part]).lower()
          if username in fetch_usernames():
              username = membership.email.split('@')[0].lower()
          if self.request.get('u'):
              pick_username = True
          message = escape(self.request.get('message'))
          account_url = str('/account/%s' % membership.hash)
          self.response.out.write(render('templates/account.html', locals()))
        else:
          self.response.out.write("404 Not Found")
    
    def post(self, hash):
        username = self.request.get('username')
        password = self.request.get('password')
        c = Config()
        if password != self.request.get('password_confirm'):
            self.redirect(str(self.request.path + "?message=Passwords don't match"))
        elif len(password) < 8:
            self.redirect(str(self.request.path + "?message=Password must be 8 characters or longer"))
        else:
            membership = Membership.get_by_hash(hash)
            if membership.username:
                self.redirect(str(self.request.path + "?message=You already have a user account"))
                return
            
            # Yes, storing their username and password temporarily so we can make their account later
            memcache.set(str(hashlib.sha1(str(membership.hash)+c.SPREEDLY_APIKEY).hexdigest()), 
                '%s:%s' % (username, password), time=3600)
            
            if membership.status == 'active':
                taskqueue.add(url='/tasks/create_user', method='POST', params={'hash': membership.hash}, countdown=3)
                self.redirect(str('http://%s/success/%s' % (self.request.host, membership.hash)))
            else:
                customer_id = membership.key().id()
                
                # This code is not weird...
                if "1337" in membership.referrer:

                    if len(membership.referrer) !=16:
                        error = "<p>Error: code must be 16 digits."
                        error += "<p>Please contact "+ SIGNUP_HELP_EMAIL+" if you believe this message is in error and we can help!"
                        error += "<p><a href='/'>Start again</a>"
                        self.response.out.write(render('templates/error.html', locals()))
                        return

                    serial = membership.referrer[4:8]
                    hash = membership.referrer[8:16]
                    confirmation_hash = re.sub('[a-f]','',hashlib.sha1(serial+keymaster.get('code:hash')).hexdigest())[:8]

                    if hash != confirmation_hash:
                        error = "<p>Error: this code was invavlid: "+ membership.referrer
                        error += "<p>Please contact "+ SIGNUP_HELP_EMAIL+" if you believe this message is in error and we can help!"
                        error += "<p><a href='/'>Start again</a>"
                        uc = UsedCode(code=membership.referrer,email=membership.email,extra="invalid code")
                        uc.put()
                        self.response.out.write(render('templates/error.html', locals()))
                        return

                    previous = UsedCode.all().filter('code =', membership.referrer).get()
                    if previous:
                        error = "<p>Error: this code has already been used: "+ membership.referrer
                        error += "<p>Please contact "+ SIGNUP_HELP_EMAIL+" if you believe this message is in error and we can help!"
                        error += "<p><a href='/'>Start again</a>"
                        uc = UsedCode(code=membership.referrer,email=membership.email,extra="2nd+ attempt")
                        uc.put()
                        self.response.out.write(render('templates/error.html', locals()))
                        return

                    headers = {'Authorization': "Basic %s" % base64.b64encode('%s:X' % c.SPREEDLY_APIKEY),
                        'Content-Type':'application/xml'}
                    # Create subscriber
                    data = "<subscriber><customer-id>%s</customer-id><email>%s</email></subscriber>" % (customer_id, membership.email)
                    resp = urlfetch.fetch("https://subs.pinpayments.com/api/v4/%s/subscribers.xml" % (c.SPREEDLY_ACCOUNT), 
                            method='POST', payload=data, headers = headers, deadline=5)
                    # Credit
                    data = "<credit><amount>95.00</amount></credit>"
                    resp = urlfetch.fetch("https://subs.pinpayments.com/api/v4/%s/subscribers/%s/credits.xml" % (c.SPREEDLY_ACCOUNT, customer_id), 
                            method='POST', payload=data, headers=headers, deadline=5)

                    uc = UsedCode(code=membership.referrer,email=membership.email,extra='OK')
                    uc.put()
                
                query_str = urllib.urlencode({'first_name': membership.first_name, 'last_name': membership.last_name, 
                    'email': membership.email, 'return_url':
                    'http://%s/success/%s' % (self.request.host, membership.hash)})
                # check if they are active already since we didn't create a new member above
                # apparently the URL will be different
                self.redirect(str("https://spreedly.com/%s/subscribers/%s/subscribe/%s/%s?%s" % 
                    (c.SPREEDLY_ACCOUNT, customer_id, c.PLAN_IDS[membership.plan], username, query_str)))

            
class CreateUserTask(webapp.RequestHandler):
    def post(self):
        def fail(exception):
            logging.error("CreateUserTask failed: %s" % exception)
            mail.send_mail(sender=EMAIL_FROM,
                to=INTERNAL_DEV_EMAIL,
                subject="[%s] CreateUserTask failure" % APP_NAME,
                body=str(exception))
        def retry(countdown=3):
            retries = int(self.request.get('retries', 0)) + 1
            if retries <= 5:
                taskqueue.add(url='/tasks/create_user', method='POST', countdown=countdown,
                    params={'hash': self.request.get('hash'), 'retries': retries})
            else:
                fail(Exception("Too many retries for %s" % self.request.get('hash')))
        
        c = Config()
        user_hash = self.request.get('hash')
        membership = Membership.get_by_hash(user_hash)
        if membership is None or membership.username:
            return
        if not membership.spreedly_token:
            logging.warn("CreateUserTask: No spreedly token yet, retrying")
            return retry(300)

            
        try:
            username, password = memcache.get(hashlib.sha1(membership.hash+c.SPREEDLY_APIKEY).hexdigest()).split(':')
        except (AttributeError, ValueError):
            return fail(Exception("Account information expired for %s" % membership.email))
            
        try:
            url = 'http://%s/users' % DOMAIN_HOST
            payload = urllib.urlencode({
                'username': username,
                'password': password,
                'first_name': membership.first_name,
                'last_name': membership.last_name,
                'secret': keymaster.get('api'),
            })
            logging.info("CreateUserTask: About to create user: "+username)
            logging.info("CreateUserTask: URL: "+url)
            logging.info("CreateUserTask: Payload: "+payload)
            resp = urlfetch.fetch(url, method='POST', payload=payload, deadline=120)
            membership.username = username
            membership.put()
            logging.warn("CreateUserTask: I think that worked: HTTP "+str(resp.status_code))

            # Send the welcome email.
            SuccessHandler.send_email(membership)
        except urlfetch.DownloadError, e:
            logging.warn("CreateUserTask: API response error or timeout, retrying")
            return retry()
        except keymaster.KeymasterError, e:
            fail(e)
            return retry(3600)
        except Exception, e:
            return fail(e)



class UnsubscribeHandler(webapp.RequestHandler):
    def get(self, id):
        member = Membership.get_by_id(int(id))
        if member:
            self.response.out.write(render('templates/unsubscribe.html', locals()))
        else:
            self.response.out.write("error: could not locate your membership record.")

    def post(self,id):
        member = Membership.get_by_id(int(id))
        if member:
            unsubscribe_reason = self.request.get('unsubscribe_reason')
            if unsubscribe_reason:
                member.unsubscribe_reason = unsubscribe_reason
                member.put()
                self.response.out.write(render('templates/unsubscribe_thanks.html', locals()))
            else:
                self.response.out.write(render('templates/unsubscribe_error.html', locals()))
        else:
            self.response.out.write("error: could not locate your membership record.")

class SuccessHandler(webapp.RequestHandler):
    @classmethod
    def send_email(cls, member):
      spreedly_url = member.spreedly_url()
      dojo_email = "%s@hackerdojo.com" % (member.username)
      name = member.full_name()
      mail.send_mail(sender=EMAIL_FROM,
          to="%s <%s>; %s <%s>" % (name, member.email, name, dojo_email),
          subject="Welcome to Hacker Dojo, %s!" % member.first_name,
          body=render('templates/welcome.txt', locals()))

    def get(self, hash):
        member = Membership.get_by_hash(hash)
        c = Config()
        if member:
          success_html = urlfetch.fetch(SUCCESS_HTML_URL).content
          success_html = success_html.replace('joining!', 'joining, %s!' % member.first_name)
          is_prod = c.is_prod
          self.response.out.write(render('templates/success.html', locals()))

class NeedAccountHandler(webapp.RequestHandler):
    def get(self):
        message = escape(self.request.get('message'))
        self.response.out.write(render('templates/needaccount.html', locals()))
    
    def post(self):
        email = self.request.get('email').lower()
        if not email:
            self.redirect(str(self.request.path))
        else:
            member = Membership.all().filter('email =', email).filter('status =', 'active').get()
            if not member:
                self.redirect(str(self.request.path + '?message=There is no active record of that email.'))
            else:
                mail.send_mail(sender=EMAIL_FROM,
                    to="%s <%s>" % (member.full_name(), member.email),
                    subject="Create your Hacker Dojo account",
                    body="""Hello,\n\nHere's a link to create your Hacker Dojo account:\n\nhttp://%s/account/%s""" % (self.request.host, member.hash))
                sent = True
                self.response.out.write(render('templates/needaccount.html', locals()))

class UpdateHandler(webapp.RequestHandler):
    def suspend(self, username):
        def fail(self, exception):
            mail.send_mail(sender=EMAIL_FROM,
                to=INTERNAL_DEV_EMAIL,
                subject="[%s] User suspension failure: " % (APP_NAME,username),
                body=str(exception))
            logging.error("User suspension failure: "+str(exception))
        try:
            resp = urlfetch.fetch('http://%s/suspend/%s' % (DOMAIN_HOST,username), method='POST', deadline=10, payload=urllib.urlencode({'secret': keymaster.get('api')}))
        except Exception, e:
            return fail(e)

    def restore(self, username):
        def fail(exception):
            mail.send_mail(sender=EMAIL_FROM,
                to=INTERNAL_DEV_EMAIL,
                subject="[%s] User restore failure: " % (APP_NAME,username),
                body=str(exception))
            logging.error("User restore failure: "+str(exception))
        try:
            resp = urlfetch.fetch('http://%s/restore/%s' % (DOMAIN_HOST,username), method='POST', deadline=10, payload=urllib.urlencode({'secret': keymaster.get('api')}))
        except Exception, e:
            return fail(e)

    def get(self):
        pass
    
    def post(self, ids=None):
        subscriber_ids = self.request.get('subscriber_ids').split(',')
        c = Config()
        s = spreedly.Spreedly(c.SPREEDLY_ACCOUNT, token=c.SPREEDLY_APIKEY)
        for id in subscriber_ids:
            subscriber = s.subscriber_details(sub_id=int(id))
            logging.debug("customer_id: "+ subscriber['customer-id'])
            member = Membership.get_by_id(int(subscriber['customer-id']))
            if member:
                if member.status == 'paypal':
                    mail.send_mail(sender=EMAIL_FROM,
                        to=PAYPAL_EMAIL,
                        subject="Please cancel PayPal subscription for %s" % member.full_name(),
                        body=member.email)
                member.status = 'active' if subscriber['active'] == 'true' else 'suspended'
                if member.status == 'active' and not member.username:
                    taskqueue.add(url='/tasks/create_user', method='POST', params={'hash': member.hash}, countdown=3)
                if member.status == 'active' and member.unsubscribe_reason:
                    member.unsubscribe_reason = None
                member.spreedly_token = subscriber['token']
                member.plan = subscriber['feature-level'] or member.plan
                if not subscriber['email']:
                  subscriber['email'] = "noemail@hackerdojo.com"
                member.email = subscriber['email']                
                member.put()
                # TODO: After a few months (now() = 06.13.2011), only suspend/restore if status CHANGED
                # As of right now, we can't trust previous status, so lets take action on each call to /update
                if member.status == 'active' and member.username:
                    logging.info("Restoring User: "+member.username)
                    self.restore(member.username)
                if member.status == 'suspended' and member.username:
                    logging.info("Suspending User: "+member.username)
                    self.suspend(member.username)

        self.response.out.write("ok")
            
class LinkedHandler(webapp.RequestHandler):
    def get(self):
        self.response.out.write(json.dumps([m.username for m in Membership.all().filter('username !=', None)]))

class APISuspendedHandler(webapp.RequestHandler):
    def get(self):
        self.response.out.write(json.dumps([[m.fullname(), m.username] for m in Membership.all().filter('status =', 'suspended')]))

class MemberListHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      if not user:
        self.redirect(users.create_login_url('/memberlist'))
      signup_users = Membership.all().order('status').order('last_name').fetch(10000);
      self.response.out.write(render('templates/memberlist.html', locals()))

class DebugHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      if not user:
        self.redirect(users.create_login_url('/debug_users'))
      if users.is_current_user_admin():
        if not self.request.get("from"):
          all_users = Membership.all()
          x = all_users.count()
          self.response.out.write("There are ")
          self.response.out.write(x)
          self.response.out.write(" user records.  Use GET params 'from' and 'to' to analyze.")
        else:
          fr = self.request.get("from")
          to = self.request.get("to")
          for i in range(int(fr),int(to)):
            a = Membership.all().fetch(1,i)[0]
            self.response.out.write("<p>")
            self.response.out.write(a.key().id())
            self.response.out.write(" - ")
            self.response.out.write(a.username)
      else:
        self.response.out.write("Need admin access")


class LeaveReasonListHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      if not user:
        self.redirect(users.create_login_url('/leavereasonlist'))
      if users.is_current_user_admin():
        all_users = Membership.all().order("-updated").fetch(10000)
        self.response.out.write(render('templates/leavereasonlist.html', locals()))
      else:
        self.response.out.write("Need admin access")
  
class JoinReasonListHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      if not user:
        self.redirect(users.create_login_url('/joinreasonlist'))
      if users.is_current_user_admin():
        all_users = Membership.all().order("created").fetch(10000)
        self.response.out.write(render('templates/joinreasonlist.html', locals()))
      else:
        self.response.out.write("Need admin access")
  
class SuspendedHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      if not user:
        self.redirect(users.create_login_url('/suspended'))
      if users.is_current_user_admin():
        suspended_users = Membership.all().filter('status =', 'suspended').filter('last_name !=', 'Deleted').fetch(10000)
        tokened_users = []
        for user in suspended_users:
            if user.spreedly_token:
                tokened_users.append(user)
        suspended_users = sorted(tokened_users, key=lambda user: user.last_name.lower())        
        total = len(suspended_users)
        reasonable = 0
        for user in suspended_users:
            if user.unsubscribe_reason:
                reasonable += 1
        self.response.out.write(render('templates/suspended.html', locals()))
      else:
        self.response.out.write("Need admin access")
        		
class AllHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      if not user:
        self.redirect(users.create_login_url('/userlist'))
      if users.is_current_user_admin():
        signup_users = Membership.all().fetch(10000)
#        active_users = Membership.all().filter('status =', 'active').fetch(10000)
#        signup_usernames = [m.username for m in signup_users]
#        domain_usernames = fetch_usernames()
#        signup_usernames = set(signup_usernames) - set([None])
#        signup_usernames = [m.lower() for m in signup_usernames]
#        active_usernames = [m.username for m in active_users]
#        active_usernames = set(active_usernames) - set([None])
#        active_usernames = [m.lower() for m in active_usernames]
#        users_not_on_domain = set(signup_usernames) - set(domain_usernames)
#        users_not_on_signup = set(domain_usernames) - set(active_usernames)
        signup_users = sorted(signup_users, key=lambda user: user.last_name.lower())
        self.response.out.write(render('templates/users.html', locals()))
      else:
        self.response.out.write("Need admin access")

class HardshipHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      if not user:
        self.redirect(users.create_login_url('/hardshiplist'))
      if users.is_current_user_admin():
        active_users = Membership.all().filter('status =', 'active').filter('plan =', 'hardship').fetch(10000)
        active_users = sorted(active_users, key=lambda user: user.created) 
        subject = "About your Hacker Dojo membership"
        body1 = "\n\nWe hope you have enjoyed your discounted membership at Hacker Dojo.  As you\nknow, we created the hardship program to give temporary financial support to help\nmembers get started at the Dojo.  Our records show you began the program\non "
        body2 = ", and we hope you feel that you have benefited.\n\nBeginning with your next month's term, we ask that you please sign up at\nour regular rate:\n"
        body3 = "\n\nThank you for supporting the Dojo!"
        self.response.out.write(render('templates/hardship.html', locals()))
      else:
        self.response.out.write("Need admin access")

      
class AreYouStillThereHandler(webapp.RequestHandler):
    def get(self):
        if not Config().is_dev:
          self.post()
        
    def post(self):
        countdown = 0
        for membership in Membership.all().filter('status =', "suspended"):
          if not membership.unsubscribe_reason and membership.spreedly_token and "Deleted" not in membership.last_name and membership.extra_dnd != True:
            countdown += 1200 # One e-mail every 20 min = 72 e-mails a day (100 is free appengine limit)
            self.response.out.write("Are you still there "+membership.email+ "?<br/>")
            taskqueue.add(url='/tasks/areyoustillthere_mail', params={'user': membership.key().id()}, countdown=countdown)

class AreYouStillThereMail(webapp.RequestHandler):
    def post(self): 
        user = Membership.get_by_id(int(self.request.get('user')))
        subject = "Hacker Dojo Membership: ACTION REQUIRED"
        body = render('templates/areyoustillthere.txt', locals())
        to = "%s <%s>" % (user.full_name(), user.email)
        bcc = "%s <%s>" % ("Billing System", "robot@hackerdojo.com")
        if user.username:
            cc="%s <%s@hackerdojo.com>" % (user.full_name(), user.username),
            mail.send_mail(sender=EMAIL_FROM_AYST, to=to, subject=subject, body=body, bcc=bcc, cc=cc)
        else:
            mail.send_mail(sender=EMAIL_FROM_AYST, to=to, subject=subject, body=body, bcc=bcc)
        
class ReactivateHandler(webapp.RequestHandler):
    def get(self):
        message = escape(self.request.get('message'))
        self.response.out.write(render('templates/reactivate.html', locals()))
    def post(self):
      email = self.request.get('email').lower()
      existing_member = db.GqlQuery("SELECT * FROM Membership WHERE email = :email", email=email).get()
      if existing_member:
          membership = existing_member
          if membership.status == "active":
              self.redirect(str(self.request.path + '?message=You are still an active member'))
          else:
            subject = "Reactivate your Hacker Dojo Membership"
            body = render('templates/reactivate.txt', locals())
            to = "%s <%s>" % (membership.full_name(), membership.email)
            bcc = "%s <%s>" % ("Billing System", "robot@hackerdojo.com")
            mail.send_mail(sender=EMAIL_FROM_AYST, to=to, subject=subject, body=body, bcc=bcc)
            sent = True
            self.response.out.write(render('templates/reactivate.html', locals()))
      else:
          self.redirect(str(self.request.path + '?message=There is no record of that email.'))
        
class CleanupHandler(webapp.RequestHandler):
    def get(self):
        self.post()
        
    def post(self):
        countdown = 0
        for membership in Membership.all().filter('status =', None):
            if (datetime.now().date() - membership.created.date()).days > 1:
                countdown += 90
                self.response.out.write("bye "+membership.email+ " ")
                taskqueue.add(url='/tasks/clean_row', params={'user': membership.key().id()}, countdown=countdown)


class CleanupTask(webapp.RequestHandler):
    def post(self): 
        user = Membership.get_by_id(int(self.request.get('user')))
        try:
          mail.send_mail(sender=EMAIL_FROM,
              to=user.email,
              subject="Hi again -- from Hacker Dojo!",
              body="Hi "+user.first_name+""",
              \nOur fancy membership system noted that you started filling out the Membership Signup form, but didn't complete it.
              \nWell -- We'd love to have you as a member!
              \nHacker Dojo has grown by leaps and bounds in recent years.  Give us a try?
              \nIf you would like to become a member of Hacker Dojo, just complete the signup process at http://signup.hackerdojo.com
              \nIf you don't want to sign up -- please give us anonymous feedback so we know how we can do better!  URL: http://bit.ly/jJAGYM
              \nCheers!\nHacker Dojo\n\nPS: Please ignore this e-mail if you already signed up -- you might have started signing up twice or something :)
              \nPSS: This is an automated e-mail and we're now deleting your e-mail address from the signup application"""
          )
        except:  
          noop = True
        user.delete()
        
        
class ProfileHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      if not user:
          self.redirect(users.create_login_url('/profile'))
          return
      else:
          account = Membership.all().filter('username =', user.nickname().split("@")[0]).get()
          email = '%s@%s' % (account.username, APPS_DOMAIN)
          gravatar_url = "http://www.gravatar.com/avatar/" + hashlib.md5(email.lower()).hexdigest()          
          self.response.out.write(render('templates/profile.html', locals()))

class PrefHandler(webapp.RequestHandler):
   def get(self):
      user = users.get_current_user()
      if not user:
          self.redirect(users.create_login_url('/pref'))
          return
      else:
          account = Membership.all().filter('username =', user.nickname().split("@")[0]).get()
          if not account:
            error = "<p>Error - couldn't find your account.</p>"
            error += "<pre>Nick: "+str(user.nickname().split("@")[0])
            error += "<pre>Email: "+str(user.email())
            error += "<pre>Account: "+str(account)
            if account:
              error += "<pre>Token: "+str(account.spreedly_token)
            self.response.out.write(render('templates/error.html', locals()))
            return
          auto_signin = account.auto_signin
          self.response.out.write(render('templates/pref.html', locals()))

   def post(self):
      user = users.get_current_user()
      if not user:
          self.redirect(users.create_login_url('/pref'))
          return
      account = Membership.all().filter('username =', user.nickname().split("@")[0]).get()
      if not account:
            error = "<p>Error #1983, which should never happen."
            self.response.out.write(render('templates/error.html', locals()))
            return
      auto_signin = self.request.get('auto').strip()
      account.auto_signin = auto_signin
      account.put()
      self.response.out.write(render('templates/prefsaved.html', locals()))
 
            

class KeyHandler(webapp.RequestHandler):
    def get(self):
        user = users.get_current_user()
        c = Config()
        message = escape(self.request.get('message'))
        if not user:
            self.redirect(users.create_login_url('/key'))
            return
        else:
            account = Membership.all().filter('username =', user.nickname().split("@")[0]).get()
            if not account or not account.spreedly_token:
                error = """<p>It appears that you have an account on @%(domain)s, but you do not have a corresponding account in the signup application.</p>
                <p>How to remedy:</p>
                <ol><li>If you <b>are not</b> in the Spreedly system yet, <a href=\"/\">sign up</a> now.</li>
                <li>If you <b>are</b> in Spreedly already, please contact <a href=\"mailto:%(signup_email)s?Subject=Spreedly+account+not+linked+to+account\">%(signup_email)s</a>.</li></ol>
                <pre>Nick: %(nick)s</pre>
                <pre>Email: %(email)s</pre>
                <pre>Account: %(account)s</pre>
                """ % {'domain': APPS_DOMAIN, 'signup_email': SIGNUP_HELP_EMAIL, 'nick': user.nickname().split("@")[0], 'email': user.email(), 'account': account}
                if account:
                    error += "<pre>Token: %s</pre>" % account.spreedly_token

                self.response.out.write(render('templates/error.html', locals()))
                return
            if account.status != "active":
                url = "https://spreedly.com/"+c.SPREEDLY_ACCOUNT+"/subscriber_accounts/"+account.spreedly_token
                error = """<p>Your Spreedly account status does not appear to me marked as active. This might be a mistake, in which case we apologize. </p>
                <p>To investigate your account, you may go here: <a href=\"%(url)s\">%(url)s</a> </p>
                <p>If you believe this message is in error, please contact <a href=\"mailto:%(signup_email)s?Subject=Spreedly+account+not+linked+to+account\">%(signup_email)s</a></p>
                """ % {'url': url, 'signup_email': SIGNUP_HELP_EMAIL}
                self.response.out.write(render('templates/error.html', locals()))
                return
            delta = datetime.utcnow() - account.created
            if delta.days < DAYS_FOR_KEY:
                error = """<p>You have been a member for %(deltadays)s days.
                After %(days)s days you qualify for a key.  Check back in %(delta)s days!</p>
                <p>If you believe this message is in error, please contact <a href=\"mailto:%(signup_email)s?Subject=Membership+create+date+not+correct\">%(signup_email)s</a>.</p>
                """ % {'deltadays': delta.days, 'days': DAYS_FOR_KEY, 'delta': DAYS_FOR_KEY-delta.days, 'signup_email': SIGNUP_HELP_EMAIL}
                self.response.out.write(render('templates/error.html', locals()))
                return
            bc = BadgeChange.all().filter('username =', account.username).fetch(100)
            pp = account.parking_pass
            self.response.out.write(render('templates/key.html', locals()))

    def post(self):
      user = users.get_current_user()
      if not user:
          self.redirect(users.create_login_url('/key'))
          return
      account = Membership.all().filter('username =', user.nickname().split("@")[0]).get()
      if not account or not account.spreedly_token or account.status != "active":
            error = "<p>Error #1982, which should never happen."
            self.response.out.write(render('templates/error.html', locals()))
            return
      is_park = self.request.get('ispark')
      if is_park == "True": #checks if user input is a parking pass number or an rfid number
        pass_to_add = self.request.get('parking_pass')
        try: #tests if there are only numbers in the parking pass
          float(pass_to_add)
        except ValueError:
          error = "<p>A Parking Pass may only contain numbers.</p><a href=\"/key\">Try Again</a>"
          self.response.out.write(render('templates/error.html', locals()))
          return
        account.parking_pass = pass_to_add
        db.put(account)
        self.response.out.write(render('templates/pass_ok.html', locals())) #outputs the parking number
      else:
        rfid_tag = self.request.get('rfid_tag').strip()
        description = self.request.get('description').strip()
        if rfid_tag.isdigit():
          if Membership.all().filter('rfid_tag =', rfid_tag).get():
            error = "<p>That RFID tag is in use by someone else.</p>"
            self.response.out.write(render('templates/error.html', locals()))
            return
          if not description:
            error = "<p>Please enter a reason why you are associating a replacement RFID key.  Please hit BACK and try again.</p>"
            self.response.out.write(render('templates/error.html', locals()))
            return
          account.rfid_tag = rfid_tag
          account.put()
          bc = BadgeChange(rfid_tag = rfid_tag, username=account.username, description=description)
          bc.put()
          self.response.out.write(render('templates/key_ok.html', locals()))
          return
        else:
          error = "<p>That RFID ID seemed invalid. Hit back and try again.</p>"
          self.response.out.write(render('templates/error.html', locals()))
          return

class RFIDHandler(webapp.RequestHandler):
    def get(self):
      if self.request.get('id'):
        m = Membership.all().filter('rfid_tag ==', self.request.get('id')).filter('status =', 'active').get()
        if self.request.get('callback'): # jsonp callback support
          self.response.out.write(self.request.get('callback')+"(");
        if m:
          email = '%s@%s' % (m.username, APPS_DOMAIN)
          gravatar_url = "http://www.gravatar.com/avatar/" + hashlib.md5(email.lower()).hexdigest()
          self.response.out.write(json.dumps({"gravatar": gravatar_url,"auto_signin":m.auto_signin, "status" : m.status, "name" : m.first_name + " " + m.last_name, "rfid_tag" : m.rfid_tag, "username" : m.username }))
        else:
          self.response.out.write(json.dumps({}))
        if self.request.get('callback'):
          self.response.out.write(")");
      else:
        if self.request.get('maglock:key') == keymaster.get('maglock:key'):
          if self.request.get('machine'):       
            members = Membership.all().filter('rfid_tag !=', None).filter('status =', 'active').filter("extra_"+self.request.get('machine')+' =',"True")
          else:
            members = Membership.all().filter('rfid_tag !=', None).filter('status =', 'active')
          self.response.out.write(json.dumps([ {"rfid_tag" : m.rfid_tag, "username" : m.username } for m in members]))
        else:
          self.response.out.write("Access denied")

class ModifyHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      c = Config()
      account = Membership.all().filter('username =', user.nickname().split("@")[0]).get()
      if not account:
          self.redirect(users.create_login_url('/modify'))
          return
      else:
          if not account or not account.spreedly_token:
            error = """<p>Sorry, your %(name)s account does not appear to be linked to a Spreedly account.  
Please contact <a href=\"mailto:%(treasurer)s\">%(treasurer)s</a> so they can manually update your account.
""" % {'treasurer': TREASURER_EMAIL, 'name': ORG_NAME}
            self.response.out.write(render('templates/error.html', locals()))
            return
          url = "https://spreedly.com/"+c.SPREEDLY_ACCOUNT+"/subscriber_accounts/"+account.spreedly_token
          self.redirect(str(url))

class GenLinkHandler(webapp.RequestHandler):
    def get(self,key):
        c = Config()
        sa = c.SPREEDLY_ACCOUNT
        u = Membership.get_by_id(int(key))
        plans = c.PLAN_IDS
        self.response.out.write(render('templates/genlink.html', locals()))
        

class CacheUsersCron(webapp.RequestHandler):
    def get(self):
        self.post()
        
    def post(self): 
        fetch_usernames(use_cache=False)
          
class GetTwitterHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      if not user:
        self.redirect(users.create_login_url('/api/gettwitter'))
      if users.is_current_user_admin():
        need_twitter_users = Membership.all().filter('status =', 'active').fetch(10000)
        countdown = 0
        for u in need_twitter_users:
          if u.username and not u.twitter:
            self.response.out.write(u.username)
            taskqueue.add(url='/tasks/twitter_mail', params={'user': u.key().id()}, countdown=countdown)
            countdown += 1
      else:
        self.response.out.write("Need admin access")

            

class TwitterMail(webapp.RequestHandler):
    def post(self): 
        user = Membership.get_by_id(int(self.request.get('user')))
        subject = "What's your twitter handle?"
        base = self.request.host
        body = render('templates/twittermail.txt', locals())
        to = "%s <%s@hackerdojo.com>" % (user.full_name(), user.username)
        bcc = "%s <%s>" % ("Robot", "robot@hackerdojo.com")
        mail.send_mail(sender=EMAIL_FROM_AYST, to=to, subject=subject, body=body, bcc=bcc, html=body)
    
class SetTwitterHandler(webapp.RequestHandler):
    def get(self):
      if self.request.get('user'):
        m = Membership.get(self.request.get('user'))
        m.twitter = self.request.get('twitter').lower().strip().strip('@')
        m.put()
        self.response.out.write("<p>Thanks!  All set now.  <p>We'll send out more information in a week or two.")

class SetHSHandler(webapp.RequestHandler):
    def get(self):
      if self.request.get('user'):
        m = Membership.get(self.request.get('user'))
        m.hardship_comment = self.request.get('comment').strip()
        m.put()
        self.response.out.write("<p>Set.")

class SetExtraHandler(webapp.RequestHandler):
    def get(self):
      user = users.get_current_user()
      if not user:
        self.redirect(users.create_login_url('/api/setextra'))
      if users.is_current_user_admin():
        user = Membership.all().filter('username =', self.request.get('username')).get()
        if user:
          v = self.request.get('value')
          if v=="True":
              v = True
          if v=="False":
              v = False
          user.__setattr__("extra_"+self.request.get('key'), v)
          user.put()
          self.response.out.write("OK")
        else:
          self.response.out.write("User not found")
      else:
        self.response.out.write("Need admin access")

class CSVHandler(webapp.RequestHandler):
    def get(self):
      self.response.headers['Content-type'] = "text/csv; charset=utf-8"
      self.response.headers['Content-disposition'] = "attachment;filename=HackerDojoMembers.csv"
      if keymaster.get('csvkey') == self.request.get('csvkey'): 
        users = Membership.all().filter('status =', 'active').filter('username !=', '').fetch(10000)
        for u in users:
          twitter = ''
          if u.twitter:
            twitter = u.twitter
          first = ''
          if u.first_name:
            first = u.first_name
          last = ''
          if u.last_name:
            last = u.last_name
          if u.username:
            self.response.out.write(first+","+last+","+u.username+"@hackerdojo.com,"+twitter+"\r\n")
 

app = webapp.WSGIApplication([
        ('/', MainHandler),
        ('/api/rfid', RFIDHandler),
        ('/api/rfidswipe', RFIDSwipeHandler),
        ('/userlist', AllHandler),
        ('/suspended', SuspendedHandler),
        ('/api/linked', LinkedHandler),
        ('/api/suspended', APISuspendedHandler),
        ('/cleanup', CleanupHandler),
        ('/profile', ProfileHandler),
        ('/debug_users', DebugHandler),
        ('/key', KeyHandler),
        ('/pref', PrefHandler),
        ('/modify', ModifyHandler),
        ('/genlink/(.+)', GenLinkHandler),
        ('/account/(.+)', AccountHandler),
        ('/upgrade/needaccount', NeedAccountHandler),
        ('/success/(.+)', SuccessHandler),
        ('/joinreasonlist', JoinReasonListHandler),
        ('/leavereasonlist', LeaveReasonListHandler),
        ('/hardshiplist', HardshipHandler),
        ('/memberlist', MemberListHandler),
        ('/areyoustillthere', AreYouStillThereHandler),
        ('/unsubscribe/(.*)', UnsubscribeHandler),
        ('/update', UpdateHandler),
        ('/api/membercsv', CSVHandler),
        ('/api/gettwitter', GetTwitterHandler),
        ('/api/setextra', SetExtraHandler),
        ('/api/settwitter', SetTwitterHandler),
        ('/api/seths', SetHSHandler),
        ('/tasks/create_user', CreateUserTask),
        ('/tasks/clean_row', CleanupTask),
        ('/cron/cache_users', CacheUsersCron),
        ('/tasks/areyoustillthere_mail', AreYouStillThereMail),
        ('/tasks/twitter_mail', TwitterMail),
        ('/reactivate', ReactivateHandler),
        
        
        ], debug=True)

