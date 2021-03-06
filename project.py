'''
The main program for the Item Catalog Web App
'''
from database_setup import User, Category, Item, Base
from flask import Flask, render_template, url_for, request, flash, \
    make_response, redirect, jsonify
from flask import session as login_session
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from oauth2client.client import flow_from_clientsecrets
from oauth2client.client import FlowExchangeError
from oauth2client.client import OAuth2Credentials
import httplib2
import random
import string
import json
import requests
import bleach


# Create the database engine instance and bind it to a session
engine = create_engine('postgresql:///catalog')
Base.metadata.bind = engine
DBSession = sessionmaker(bind=engine)
session = DBSession()


app = Flask(__name__)
app.secret_key = 'super_secret_key'

G_CLIENT_ID = json.loads(open('g_client_secrets.json', 'r').read(
    ))['web']['client_id']


@app.route('/login/')
def login():
    state = ''.join(random.choice(string.ascii_uppercase +
                                  string.digits)
                    for x in xrange(32))
    login_session['state'] = state
    return render_template('login.html',
                           login_session=login_session,
                           state=state)


@app.route('/gconnect/', methods=['POST'])
def gconnect():
    if request.args.get('state') != login_session['state']:
        response = make_response(json.dumps('Invalid state '
                                            'parameter'),
                                 401)
        response.headers['Content-Type'] = 'application/json'
        return response
    code = request.data
    try:
        # Exchange the auth code for a credentials object
        oauth_flow = flow_from_clientsecrets(
            'g_client_secrets.json',
            scope='https://www.googleapis.com/auth/userinfo.email')
        oauth_flow.redirect_uri = 'postmessage'
        credentials = oauth_flow.step2_exchange(code)
    except FlowExchangeError:
        response = make_response(
            json.dumps('Failed to get credentials from Google'), 401)
        response.headers['Content-Type'] = 'application/json'
        return response
    # Check that the access token is valid
    access_token = credentials.access_token
    url = 'https://www.googleapis.com/oauth2/v1/tokeninfo?' \
          'access_token={}'.format(access_token)
    h = httplib2.Http()
    result = json.loads(h.request(url, 'GET')[1])
    # If there are errors in the access token, abort
    if result.get('error') is not None:
        response = make_response(
            json.dumps(result.get('error'), 500))
        response.headers['Content-Type'] = 'application/json'
        return response
    # The token is valid. Make sure it's for the right user
    gplus_id = credentials.id_token['sub']
    if result['user_id'] != gplus_id:
        response = make_response(
            json.dumps('Tokens user ID does not match given user id.'),
            401
        )
        response.headers['Content-Type'] = 'application/json'
        return response
    # Verify that the access token is valid for this app
    if result['issued_to'] != G_CLIENT_ID:
        response = make_response(
            json.dumps('Tokens client ID does not apps.'),
            401
        )
        response.headers['Content-Type'] = 'application/json'
        return response
    # Check to see if user is already logged in
    stored_credentials = login_session.get('credentials')
    if stored_credentials is not None:
        # Get a credentials object from the stored json.
        # The OAuth2Credentials class is not serializable
        stored_credentials = OAuth2Credentials.from_json(stored_credentials)
    stored_gplus_id = login_session.get('gplus_id')
    if stored_credentials is not None and gplus_id == stored_gplus_id:
        response = make_response(
            json.dumps('Current user is already connected'),
            200
        )
        response.headers['Content-Type'] = 'application/json'
        return response
    # Store the access token in the session for later use
    login_session['credentials'] = credentials.to_json()
    login_session['gplus_id'] = gplus_id
    # Get user info
    userinfo_url = 'https://www.googleapis.com/oauth2/v1/userinfo'
    params = {
        'access_token': credentials.access_token,
        'alt': 'json'
    }
    answer = requests.get(userinfo_url, params=params)
    data = json.loads(answer.text)
    login_session['provider'] = 'google'
    login_session['username'] = data['name']
    login_session['picture'] = data['picture']
    login_session['email'] = data['email']
    # Create a User object in the database for this user if one does not exist
    user_id = get_user_id(login_session['email'])
    if not user_id:
        user_id = create_user(login_session)
    login_session['user_id'] = user_id
    # Print the welcome message to the user
    output = ''
    output += '<h1>Welcome, '
    output += login_session['username']
    output += '!</h1>'
    output += 'You are user number {}'.format(user_id)
    output += '<img src="'
    output += login_session['picture']
    output += '" style="width: 300px; height: 300px;'
    output += 'border-radius: 150px;'
    output += '-webkit-border-radius: 150px;'
    output += '-moz-border-radius: 150px;">'
    return output


@app.route('/gdisconnect/')
def gdisconnect():
    # Only disconnect a connected user
    credentials = login_session.get('credentials')
    if not credentials:
        response = make_response(json.dumps(
            'Current user not connected',
            401))
        response.headers['Content-Type'] = 'application/json'
        return response
    else:
        # Get the credentials object from the JSON string
        credentials = OAuth2Credentials.from_json(credentials)

    # Tell Google to revoke the access token
    access_token = credentials.access_token
    url = 'https://accounts.google.com/o/oauth2/revoke?token={}'.format(
        access_token)
    h = httplib2.Http()
    result = h.request(url, 'GET')[0]

    # If successful, delete the users session info
    if result['status'] == '200':
        response = make_response(json.dumps('Successfully disconnected.'),
                                 200)
        response.headers['Content-Type'] = 'application/json'
        return response
    else:
        # The given token was invalid
        response = make_response(json.dumps('Failed to revoke for given user'),
                                 400)
        response.headers['Content-Type'] = 'application/json'
        return response


# Disconnect based on provider
@app.route('/logout/')
def logout():
    if 'provider' in login_session:
        gdisconnect()
        del login_session['gplus_id']
        del login_session['credentials']
        del login_session['username']
        del login_session['email']
        del login_session['picture']
        del login_session['user_id']
        del login_session['provider']
        flash('You have successfully been logged out.')
        return redirect(url_for('catalog_main'))
    else:
        flash('You were not logged in')
        return redirect(url_for('catalog_main'))


@app.route('/')
@app.route('/catalog/')
def catalog_main():
    # Get the three latest items to display and all of the categories
    newest_items = session.query(Item).order_by(Item.id.desc()).limit(3)
    categories = session.query(Category).all()
    return render_template('catalog.html',
                           login_session=login_session,
                           categories=categories,
                           items=newest_items)


@app.route('/catalog/category/<int:category_id>/')
def category_main(category_id):
    # Get all of the categories so they can be shown.
    # Also get the current category so it can be marked in the
    # rendered page
    categories = session.query(Category).all()
    category = session.query(Category).filter_by(id=category_id).one()
    category_items = session.query(Item).filter_by(category_id=category_id)
    return render_template('category.html',
                           login_session=login_session,
                           category=category,
                           categories=categories,
                           items=category_items)


@app.route('/catalog/categories/JSON/')
def categories_json():
    categories = session.query(Category).all()
    return jsonify(Categories=[cat.serialize for cat in categories])


@app.route('/catalog/items/JSON/')
def items_json():
    items = session.query(Item).all()
    return jsonify(Items=[item.serialize for item in items])


@app.route('/catalog/item/<int:item_id>/')
def item_main(item_id):
    # Show the item info place
    categories = session.query(Category).all()
    item = session.query(Item).filter_by(id=item_id).one()
    return render_template('item.html',
                           login_session=login_session,
                           categories=categories,
                           item=item)


@app.route('/catalog/item/add/', methods=['GET', 'POST'])
def item_add():
    if 'user_id' not in login_session:
        flash('User must login to complete the given action.')
        return redirect(url_for('login'))
    if request.method == 'POST':
        # Get the category ID from the name
        category = session.query(Category).filter_by(
            name=request.form['category']).one()
        new_item = Item(name=bleach.clean(request.form['name']),
                        category_id=category.id,
                        description=bleach.clean(request.form['description']),
                        user_id=login_session['user_id'])
        session.add(new_item)
        session.commit()
        flash('Item added.')
        return redirect(url_for('catalog_main'))
    else:
        categories = session.query(Category).all()
        return render_template('itemAdd.html', login_session=login_session,
                               categories=categories)


@app.route('/catalog/item/<int:item_id>/edit/', methods=['GET', 'POST'])
def item_edit(item_id):
    if 'user_id' not in login_session:
        flash('User must login to complete the given action.')
        return redirect(url_for('login'))
    item = session.query(Item).filter_by(id=item_id).one()
    if login_session.get('user_id') != item.user_id:
        flash('Unauthorized to edit item {}.'.format(item_id))
        return redirect(url_for('catalog_main'))
    if request.method == 'POST':
        if request.form['name']:
            item.name = bleach.clean(request.form['name'])
        if request.form['category']:
            # Get the category ID from the category name
            category = session.query(Category).filter_by(
                name=request.form['category']).one()
            item.category_id = category.id
        if request.form['description']:
            item.price = bleach.clean(request.form['description'])
        session.add(item)
        session.commit()
        flash('Item {} edited.'.format(item_id))
        return redirect(url_for('catalog_main'))
    else:
        categories = session.query(Category).all()
        return render_template('itemEdit.html', login_session=login_session,
                               item=item, categories=categories)


@app.route('/catalog/item/<int:item_id>/delete/', methods=['GET', 'POST'])
def item_delete(item_id):
    if 'user_id' not in login_session:
        flash('User must login to complete the given action.')
        return redirect(url_for('login'))
    item = session.query(Item).filter_by(id=item_id).one()
    if login_session.get('user_id') != item.user_id:
        flash('Unauthorized to delete item {}.'.format(item_id))
        return redirect(url_for('catalog_main'))
    if request.method == 'POST':
        session.delete(item)
        session.commit()
        flash('Item {} deleted.'.format(item_id))
        return redirect(url_for('catalog_main'))
    else:
        return render_template('itemDelete.html', login_session=login_session,
                               item=item)


def get_user_id(email):
    # Gets the user ID from the user's email address
    try:
        user = session.query(User).filter_by(email=email).one()
        return user.id
    except:
        return None


def create_user(login_session):
    # Creates a new user in the database and returns its ID
    new_user = User(name=login_session['username'],
                    email=login_session['email'],
                    picture=login_session['picture'])
    session.add(new_user)
    session.commit()
    return new_user.id


if __name__ == '__main__':
    app.debug = True 
    app.run()
