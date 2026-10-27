from app.users import google_username_from_email


def test_google_username_from_email():
    assert google_username_from_email('Alice.Smith+demo@gmail.com') == 'alicesmith'
    assert google_username_from_email('my.user_name@example.com') == 'myuser_name'
    assert google_username_from_email('user@example.com') == 'user'
