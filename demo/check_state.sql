.headers on
.mode column

.print ''
.print 'users'
SELECT id, name, email, token
FROM users
ORDER BY id;

.print ''
.print 'audit'
SELECT id, user_id, message
FROM audit
ORDER BY id;

.print ''
.print 'accounts'
SELECT id, user_id, balance
FROM accounts
ORDER BY id;

.print ''
.print 'settings'
SELECT id, value
FROM settings
ORDER BY id;
