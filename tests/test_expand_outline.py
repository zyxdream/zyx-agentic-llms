# cwd set to lexintel, not tests (under run-Edit configuration)
import os
print("cwd: ", os.getcwd())

from backend.answer_drafter import expand_outline

def test_simple():
    intext = """
III. FACTUAL ALLEGATIONS
A. Digital Envoy and its Technology
8-10
B. Google Background
18-19
"""
    # expected output: [('III. FACTUAL ALLEGATIONS', None), ('A. Digital Envoy and its Technology', None), ('Paragraph 8', 8), ('Paragraph 9', 9), ('Paragraph 10', 10), ('B. Google Background', None), ('Paragraph 11', 11), ('Paragraph 12', 12), ('Paragraph 13', 13), ('Paragraph 14', 14), ('Paragraph 15', 15), ('Paragraph 16', 16), ('Paragraph 17', 17), ('Paragraph 18', 18), ('Paragraph 19', 19)]

    expanded, total_paragraphs = expand_outline(intext)
    print(expanded)
    assert isinstance(expanded, list)
    assert expanded[0] == ("III. FACTUAL ALLEGATIONS", None)
    assert expanded[2] == ('Paragraph 8', 8)
    assert expanded[4] == ('Paragraph 10', 10)
    assert expanded[-1] == ('Paragraph 19', 19)
    assert expanded[-3] == ('Paragraph 17', 17)

    intext = """
    III. FACTUAL ALLEGATIONS
    A. Digital Envoy and its Technology
    1-2
    output : [('III. FACTUAL ALLEGATIONS', None), ('A. Digital Envoy and its Technology', None), ('Paragraph 1', 1), ('Paragraph 2', 2)]
    """
    expanded, total_paragraphs = expand_outline(intext)
    print(expanded)
    assert isinstance(expanded, list)
    assert expanded[0] == ("III. FACTUAL ALLEGATIONS", None)
    assert expanded[2] == ('Paragraph 1', 1)
    assert expanded[3] == ('Paragraph 2', 2)

    intext = """
III. FACTUAL ALLEGATIONS
A. Digital Envoy and its Technology
8-10
B. Google Background
18
C. Google's use of Digital Envoy's technology
21
"""
    # output: [('III. FACTUAL ALLEGATIONS', None), ('A. Digital Envoy and its Technology', None), ('Paragraph 8', 8), ('Paragraph 9', 9), ('Paragraph 10', 10), ('B. Google Background', None), ('Paragraph 11', 11), ('Paragraph 12', 12), ('Paragraph 13', 13), ('Paragraph 14', 14), ('Paragraph 15', 15), ('Paragraph 16', 16), ('Paragraph 17', 17), ('Paragraph 18', 18), ("C. Google's use of Digital Envoy's technology", None), ('Paragraph 19', 19), ('Paragraph 20', 20), ('Paragraph 21', 21)]

    expanded, total_paragraphs = expand_outline(intext)
    print(expanded)
    assert isinstance(expanded, list)
    assert expanded[0] == ("III. FACTUAL ALLEGATIONS", None)
    assert expanded[2] == ('Paragraph 8', 8)
    assert expanded[6] == ('Paragraph 11', 11)
    assert expanded[-2] == ('Paragraph 20', 20)

    intext = """
    III. FACTUAL ALLEGATIONS
    A. Digital Envoy and its Technology
    3
    B. Google Background
    5
    C. Google's use of Digital Envoy's technology
    7
    """

    #     output: [('III. FACTUAL ALLEGATIONS', None), ('A. Digital Envoy and its Technology', None), ('Paragraph 3', 3),
    #  ('B. Google Background', None), ('Paragraph 4', 4), ('Paragraph 5', 5),
    #  ("C. Google's use of Digital Envoy's technology", None), ('Paragraph 6', 6), ('Paragraph 7', 7)]

    expanded, total_paragraphs = expand_outline(intext)
    print(expanded)
    assert isinstance(expanded, list)
    assert expanded[0] == ("III. FACTUAL ALLEGATIONS", None)
    assert expanded[4] == ('Paragraph 4', 4)


def test_outline():
    assert True

test_simple()