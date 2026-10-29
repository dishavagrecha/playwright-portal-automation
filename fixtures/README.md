# Offline HTML fixtures

`github-release-form.html` is a snapshot of the account holder's actual GitHub new-release form, captured without publishing. Before saving it, the runner removed scripts, hidden controls, URLs, values, and account strings. `github-release-targets.json` describes five controls on that form. The production `Resolver.find_in_html` reads this HTML, including when the form is wrapped in an iframe.

`form.html` and `targets.json` are smaller synthetic fixtures for a three-field form inside an iframe. They exercise the same resolver in isolation.
