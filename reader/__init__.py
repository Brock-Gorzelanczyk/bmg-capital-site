"""Evidence-first filing reader for the BMG Capital Company Report.

Pipeline (all deterministic, no language model):

    document.load()   bytes -> Document (pages, printed page numbers,
                      sections, boilerplate removed, warnings)
    tables.parse()    Document -> table rows with period, scale, unit,
                      statement, column heading and source location
    facts.extract()   rows -> reconciled facts with evidence, checks and
                      calculations
    profiles / risks  business metrics and risk findings with evidence
    report.build()    everything -> one validated report dict
    pdf.render()      report dict -> PDF bytes (same data as the screen)

Document contents are treated as data only: nothing in a filing can change
what this code does.
"""
