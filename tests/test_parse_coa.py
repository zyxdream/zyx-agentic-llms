
from backend.util.data_tools import remove_empty_rows, parse_coas

def test_simple():
    intext = """
(Direct Copyright Infringement): Since the defendant directly downloaded and used the copyrighted screenplay without any form of authorization, this serves as a strong cause of action.

(Contributory Copyright Infringement): The defendant's actions in directing the general public on how to obtain and download the copyright-infringing PDF copy of the unreleased screenplay constitute contributory copyright infringement.

(Vicarious Copyright Infringement): By creating an environment, through their digital platform, which facilitated the unlawful distribution of a copyrighted screenplay, the defendant can be held liable for vicarious copyright infringement. 

(Violation of the Digital Millennium Copyright Act (DMCA)): The defendant failed to comply with a submitted DMCA Notice of Infringement, a legal avenue provided to copyright owners to protect their work online, which serves as a clear cause of action.

(Tortious Interference with Prospective Economic Advantage): As the plaintiff routinely sells screenplays for money, the defendant's actions, which compromised the screenplay's exclusivity, can potentially be seen as an interference with the plaintiff’s prospective economic advantage. 

(Invasion of Privacy): Given the defendant's unauthorized access, dissemination, and publicizing of the plaintiff's unreleased work, the plaintiff may have a cause of action for invasion of privacy.
"""

    # filter out empty rows
    full_rows = remove_empty_rows(intext=intext)
    assert len(full_rows) == 6

    coas = parse_coas(intext=intext)

    assert coas[0][0] == "(Direct Copyright Infringement)"
    assert coas[5][0] == "(Invasion of Privacy)"
