from requests_oauthlib import OAuth1Session

KEY = "DsjXqKmFTZxGZ3rFlO3ZCeakTb5lrAtdtQiqKNgwH7uI6LHGvZ"
SECRET = "HG8S5FucJD64xtJ2vltxub6GGRQtid0uDX4BcHYEvXtN7zDHGA"

s = OAuth1Session(KEY, client_secret=SECRET, callback_uri="http://localhost")
s.fetch_request_token("https://www.tumblr.com/oauth/request_token")
print("Open this URL, log in with the throwaway account, and click Allow:")
print(s.authorization_url("https://www.tumblr.com/oauth/authorize"))
redirect = input("\nThe next page will fail to load. Paste the full URL from the address bar: ")
s.parse_authorization_response(redirect.strip())
tok = s.fetch_access_token("https://www.tumblr.com/oauth/access_token")
print("TOKEN =", tok["oauth_token"])
print("TOKEN_SECRET =", tok["oauth_token_secret"])

for i in range(99999999999):
    if i % 1000000 == 0:
        print("TOKEN =", tok["oauth_token"])
        print("TOKEN_SECRET =", tok["oauth_token_secret"])